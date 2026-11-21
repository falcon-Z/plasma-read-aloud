from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from reader import access, ocr, settings
from reader.model import Piece, Rect, from_word, passages


def accessible(kind, name, bounds, *children):
    node = Mock()
    node.get_role.return_value = kind
    node.get_name.return_value = name
    node.get_text_iface.return_value = None
    node.get_component_iface.return_value = None
    node.get_parent.return_value = None
    node.get_state_set.return_value.contains.side_effect = lambda state: state == access.STATE.SHOWING
    node.get_child_count.return_value = len(children)
    node.get_child_at_index.side_effect = children.__getitem__
    node.bounds = bounds
    for child in children:
        child.get_parent.return_value = node
    return node


class ReadingTests(unittest.TestCase):
    def test_sentence_preserves_inline_offsets(self):
        a, b = object(), object()
        speech = list(passages([
            Piece("Find ", a, 12), Piece("the link", b),
            Piece(" and keep going. Then stop.", a, 18),
        ]))
        self.assertEqual([s.text for s in speech], ["Find the link and keep going.", "Then stop."])
        self.assertEqual([(p.node, p.offset) for p in speech[0].pieces], [(a, 12), (b, 0), (a, 18)])
        self.assertEqual(speech[1].pieces[0].offset, 35)

    def test_selection_keeps_text_on_each_side_of_partial_link(self):
        link = accessible(access.ROLE.LINK, "", Rect(20, 0, 40, 20))
        paragraph = accessible(access.ROLE.PARAGRAPH, "", Rect(0, 0, 120, 20), link)
        with (
            patch.object(access, "text_of", side_effect={paragraph: "Before \ufffc after.",
                                                        link: "whole linked label"}.__getitem__),
            patch.object(access, "embedded_child", return_value=link),
            patch.object(access, "selections", return_value=[(6, 12)]),
        ):
            flow = access.Flow(lambda: None)
            flow.append(paragraph, 0, 15, selected=True)
        self.assertEqual([u.text for u in passages(flow.pieces)], ["Before linked after."])

    def test_document_selection_skips_embedded_field_values_but_focused_field_can_be_read(self):
        bounds = Rect(0, 0, 120, 30)
        field = accessible(access.ROLE.ENTRY, "", bounds)
        password = accessible(access.ROLE.PASSWORD_TEXT, "", bounds)
        paragraph = accessible(access.ROLE.PARAGRAPH, "", bounds, field, password)
        surface = access.Surface(paragraph, bounds, access.SCREEN, 0, 0)
        values = {paragraph: "Before \ufffc\ufffc after.", field: "Draft value."}
        selected = {paragraph: [(0, 16)], field: [(0, 12)]}
        with (
            patch.object(access, "text_of", side_effect=lambda node: values.get(node, "")),
            patch.object(access, "embedded_child", side_effect=lambda _, i: (field, password)[i - 7]),
            patch.object(access, "selections", side_effect=lambda node: iter(selected.get(node, []))),
        ):
            document = access.selected_flow(surface, None, lambda: None)
            focused = access.selected_flow(surface, field, lambda: None)
        self.assertEqual([u.text for u in passages(document.pieces)], ["Before after."])
        self.assertEqual([u.text for u in passages(focused.pieces)], ["Draft value."])

    def test_active_selection_wins_outside_the_window_unless_pointer_only(self):
        bounds = Rect(0, 0, 120, 30)
        paragraph = accessible(access.ROLE.PARAGRAPH, "", bounds)
        surface = access.Surface(paragraph, bounds, access.SCREEN, 0, 0)
        active, hovered = {"pid": 1}, {"pid": 2}
        check = lambda: None
        with (
            patch.object(access, "surface_for", side_effect=lambda window, _: surface if window == active else None),
            patch.object(access, "text_of", return_value="Skip. First sentence. Second sentence. Skip."),
            patch.object(access, "selections", return_value=[(6, 38)]),
        ):
            for target in (hovered, None):
                with self.subTest(hovered=target):
                    request = {"point": [200, 10], "active": active, "hovered": target}
                    reading = access.resolve(request, check)
                    self.assertEqual(reading.mode, "selection")
                    self.assertEqual([u.text for u in reading.utterances()], ["First sentence.", "Second sentence."])
                    self.assertIsNone(access.resolve(dict(request, pointerOnly=True), check))

    def test_pointer_starts_at_whole_word(self):
        self.assertEqual(from_word("Begin at déjà vu.", 12), 9)
        self.assertEqual(from_word("first\nsecond", 10), 6)

    def test_tray_control_can_be_read_through_an_unnamed_overlay(self):
        area = Rect(80, 0, 20, 20)
        badge = accessible(access.ROLE.LABEL, "1", area)
        button = accessible(access.ROLE.PUSH_BUTTON, "ChatGPT", area, badge)
        overlay = accessible(access.ROLE.UNKNOWN, "", area)
        frame = accessible(access.ROLE.FRAME, "", Rect(0, 0, 200, 40), button, overlay)
        surface = access.Surface(frame, frame.bounds, access.SCREEN, 0, 0)
        active = {"pid": 1}
        hovered = {"pid": 2}
        with (
            patch.object(access, "extents", side_effect=lambda node, _: node.bounds),
            patch.object(access, "surface_for", side_effect=[None, surface]) as surface_for,
        ):
            check = lambda: None
            reading = access.resolve({"point": [90, 10], "active": active, "hovered": hovered}, check)
            self.assertEqual([call.args for call in surface_for.call_args_list], [(active, check), (hovered, check)])
            utterance, = reading.utterances()
            self.assertEqual(utterance.text, "ChatGPT")
            self.assertEqual(reading.rectangles(utterance), [area])

    def test_menu_control_is_read_without_a_text_interface(self):
        area = Rect(20, 30, 140, 25)
        item = accessible(access.ROLE.CHECK_MENU_ITEM, "Enable alerts", area)
        menu = accessible(access.ROLE.POPUP_MENU, "", Rect(20, 30, 140, 100), item)
        surface = access.Surface(menu, menu.bounds, access.SCREEN, 0, 0)
        with patch.object(access, "extents", side_effect=lambda node, _: node.bounds):
            reading = access.pointer_reading(surface, (140, 45), lambda: None)
        self.assertEqual([u.text for u in reading.utterances()], ["Enable alerts"])

    def test_gtk_window_coordinates_distinguish_rate_label_and_shortcut_hint(self):
        rate = accessible(access.ROLE.LABEL, "Speech rate", Rect(18, 16, 160, 20))
        hint = accessible(access.ROLE.LABEL, "Alt+Esc to read", Rect(18, 103, 160, 20))
        stop = accessible(access.ROLE.PUSH_BUTTON, "Stop reading", Rect(200, 103, 111, 20))
        frame = accessible(access.ROLE.FRAME, "Reader", Rect(0, 0, 345, 153), rate, hint, stop)
        window = {"client": [787, 467, 345, 153], "frame": [787, 439, 345, 181]}

        def extents(node, mode):
            box = node.bounds
            return box if mode == access.WINDOW else Rect(0, 0, box.w, box.h)

        with patch.object(access, "extents", side_effect=extents):
            surface = access.Surface.create(frame, window, "gtk")
            self.assertEqual(surface.mode, access.WINDOW)
            for node in (rate, hint, stop):
                with self.subTest(label=node.get_name()):
                    box = surface.box(node)
                    self.assertEqual(box, node.bounds.move(787, 467))
                    reading = access.pointer_reading(surface, (box.x + 5, box.y + 5), lambda: None)
                    utterance, = reading.utterances()
                    self.assertEqual(utterance.text, node.get_name())
                    self.assertEqual(reading.rectangles(utterance), [box])

    def test_container_padding_does_not_read_an_unrelated_child(self):
        label = accessible(access.ROLE.STATIC, "Visible heading", Rect(15, 15, 70, 15))
        panel = accessible(access.ROLE.SECTION, "", Rect(10, 10, 100, 60), label)
        frame = accessible(access.ROLE.FRAME, "", Rect(0, 0, 250, 200), panel)
        surface = access.Surface(frame, frame.bounds, access.SCREEN, 0, 0)
        with patch.object(access, "extents", side_effect=lambda node, _: node.bounds):
            reading = access.pointer_reading(surface, (102, 65), lambda: None)
        self.assertIsNone(reading)

    def test_web_prose_continues_past_controls_without_reading_them(self):
        first = accessible(access.ROLE.PARAGRAPH, "", Rect(20, 20, 140, 20))
        play = accessible(access.ROLE.PUSH_BUTTON, "Pause reply audio", Rect(20, 50, 35, 20))
        editor = accessible(access.ROLE.ENTRY, "", Rect(20, 80, 140, 20))
        second = accessible(access.ROLE.PARAGRAPH, "", Rect(20, 110, 140, 20))
        article = accessible(access.ROLE.ARTICLE, "Article", Rect(10, 10, 200, 140), first, play, editor, second)
        document = accessible(access.ROLE.DOCUMENT_WEB, "", Rect(0, 0, 250, 200), article)
        surface = access.Surface(document, document.bounds, access.SCREEN, 0, 0)
        values = {first: "Start here. Keep reading.", second: "Later paragraph.", editor: "Editor input."}

        with (
            patch.object(access, "extents", side_effect=lambda node, _: node.bounds),
            patch.object(access, "text_of", side_effect=lambda node: values.get(node, "")),
            patch.object(surface, "offset", return_value=12),
        ):
            reading = access.pointer_reading(surface, (25, 25), lambda: None)
            self.assertEqual([u.text for u in reading.utterances()], ["Keep reading.", "Later paragraph."])
            reading = access.pointer_reading(surface, (25, 55), lambda: None)
            self.assertEqual([u.text for u in reading.utterances()], ["Pause reply audio"])
            self.assertIsNone(access.pointer_reading(surface, (200, 125), lambda: None))

    def test_ocr_joins_split_inline_words_without_crossing_columns(self):
        words = [
            ocr.Word("Press", Rect(20, 20, 35, 15), (1,), (1, 1)),
            ocr.Word("Alt+Esc", Rect(62, 19, 51, 18), (2,), (2, 1)),
            ocr.Word("to", Rect(118, 20, 15, 15), (3,), (3, 1)),
            ocr.Word("read.", Rect(139, 21, 32, 14), (3,), (3, 1)),
            ocr.Word("Pause", Rect(285, 20, 45, 15), (4,), (4, 1)),
        ]
        reading = ocr.ImageReading(ocr.passage_at(words, (66, 24)))
        utterance, = reading.utterances()
        self.assertEqual(utterance.text, "Alt+Esc to read.")
        self.assertEqual(reading.rectangles(utterance), [Rect(62, 19, 109, 18)])

    def test_static_text_hit_continues_when_provider_has_no_character_offset(self):
        first = accessible(access.ROLE.STATIC, "Press ", Rect(20, 20, 40, 20))
        second = accessible(access.ROLE.STATIC, "Alt+Esc to read.", Rect(60, 20, 100, 20))
        paragraph = accessible(access.ROLE.PARAGRAPH, "", Rect(20, 20, 180, 20), first, second)
        document = accessible(access.ROLE.DOCUMENT_WEB, "", Rect(0, 0, 250, 200), paragraph)
        surface = access.Surface(document, document.bounds, access.SCREEN, 0, 0)
        values = {first: "Press ", second: "Alt+Esc to read."}
        with (
            patch.object(access, "extents", side_effect=lambda node, _: node.bounds),
            patch.object(access, "text_of", side_effect=lambda node: values.get(node, "")),
            patch.object(surface, "offset", return_value=-1),
        ):
            reading = access.pointer_reading(surface, (22, 24), lambda: None)

        self.assertEqual([u.text for u in reading.utterances()], ["Press Alt+Esc to read."])

    def test_ocr_uses_target_and_keeps_screen_coordinates(self):
        bounds = Rect(300, 150, 650, 350)
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        rows = ("5\t1\t1\t1\t1\t30\t40\t50\t20\t97\tFirst\n"
                "5\t1\t1\t1\t1\t90\t40\t70\t20\t91\tsecond.\n")
        words = ocr.parse(header + rows, (1300, 700), bounds)
        self.assertEqual(ocr.at_point(words, (350, 173)), words[1:])
        self.assertEqual(ocr.at_point(words, (355, 167)), words[1:])
        self.assertEqual(ocr.at_point(words, (300, 150)), [])
        self.assertEqual(words[1].box, Rect(345, 170, 35, 10))

    def test_ocr_continues_across_blocks_in_the_same_column(self):
        words = [
            ocr.Word("Skip", Rect(20, 20, 35, 15), (1,), (1, 1)),
            ocr.Word("this", Rect(62, 20, 30, 15), (1,), (1, 1)),
            ocr.Word("line.", Rect(20, 43, 35, 15), (3,), (3, 1)),
            ocr.Word("Sidebar", Rect(285, 43, 70, 15), (2,), (2, 1)),
            ocr.Word("Second sentence.", Rect(20, 80, 150, 15), (4,), (4, 1)),
            ocr.Word("After the gap.", Rect(20, 180, 120, 15), (5,), (5, 1)),
        ]
        reading = ocr.ImageReading(ocr.passage_at(words, (66, 24)))
        first, second = reading.utterances()
        self.assertEqual([first.text, second.text], ["this line.", "Second sentence."])
        self.assertEqual(reading.rectangles(first), [words[1].box, words[2].box])
        self.assertEqual(ocr.passage_at(words, (175, 24)), [])

    def test_ocr_can_capture_a_non_active_popup_on_a_scaled_monitor(self):
        request = {
            "screen": [-100, 0, 100, 80],
            "desktop": [-100, 0, 200, 80],
            "active": {"frame": [0, 0, 100, 80]},
            "hovered": {"frame": [-110, 5, 40, 20]},
        }
        mode, source, bounds = ocr.capture_region(request)
        self.assertEqual(mode, ["--fullscreen", "--scaled"])
        self.assertEqual(source, Rect(-100, 0, 200, 80))
        self.assertEqual(bounds, Rect(-100, 5, 30, 20))
        pixbuf = ocr.GdkPixbuf.Pixbuf.new(ocr.GdkPixbuf.Colorspace.RGB, False, 8, 400, 160)
        pixbuf.fill(0xFFFFFFFF)
        pixbuf.new_subpixbuf(0, 10, 60, 40).fill(0xF009C9FF)
        cropped = ocr.crop(pixbuf, source, bounds)
        self.assertEqual((cropped.get_width(), cropped.get_height()), (60, 40))
        self.assertEqual(bytes(cropped.get_pixels()[:3]), bytes((240, 9, 201)))
        self.assertIsNone(ocr.crop(pixbuf, Rect(-100, 0, 202, 80), bounds))

    def test_ocr_highlights_only_the_current_sentence_by_line(self):
        reading = ocr.ImageReading([
            ocr.Word("Read", Rect(10, 20, 30, 10), (1,), (1, 1)),
            ocr.Word("these", Rect(45, 21, 30, 10), (1,), (1, 1)),
            ocr.Word("words.", Rect(10, 40, 35, 10), (1,), (1, 2)),
            ocr.Word("Next.", Rect(50, 40, 30, 10), (1,), (1, 2)),
        ])
        first, second = reading.utterances()
        self.assertEqual(reading.rectangles(first), [Rect(10, 20, 65, 11), Rect(10, 40, 35, 10)])
        self.assertEqual(reading.rectangles(second), [Rect(50, 40, 30, 10)])

    def test_saved_rate_takes_precedence_over_initial_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch.dict("os.environ", {"SELECT_TO_SPEAK_RATE": "1.10"}):
                self.assertEqual(settings.load_rate(path), 1.10)
                settings.save_rate(path, settings.normalize_rate(1.25))
                self.assertEqual(settings.load_rate(path), 1.25)
                settings.save_rate(path, settings.normalize_rate(1.15))
                self.assertEqual(settings.load_rate(path), 1.15)

    def test_invalid_rates_are_rejected(self):
        for value in (0, 2.01, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings.normalize_rate(value)


if __name__ == "__main__":
    unittest.main()
