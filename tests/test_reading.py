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
        paragraph, link = object(), object()
        with (
            patch.object(access, "role", side_effect={paragraph: access.ROLE.PARAGRAPH,
                                                     link: access.ROLE.LINK}.__getitem__),
            patch.object(access, "text_of", side_effect={paragraph: "Before \ufffc after.",
                                                        link: "whole linked label"}.__getitem__),
            patch.object(access, "embedded_child", return_value=link),
            patch.object(access, "selections", return_value=[(6, 12)]),
        ):
            flow = access.Flow(lambda: None)
            flow.append(paragraph, 0, 15, selected=True)
        self.assertEqual([u.text for u in passages(flow.pieces)], ["Before linked after."])

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
            patch.object(access, "surface_for", return_value=surface) as surface_for,
        ):
            check = lambda: None
            reading = access.resolve({"point": [90, 10], "active": active, "hovered": hovered}, check)
            surface_for.assert_called_once_with(hovered, check)
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

    def test_container_padding_reads_visible_child_content(self):
        label = accessible(access.ROLE.STATIC, "Visible heading", Rect(15, 15, 70, 15))
        panel = accessible(access.ROLE.SECTION, "", Rect(10, 10, 100, 60), label)
        frame = accessible(access.ROLE.FRAME, "", Rect(0, 0, 250, 200), panel)
        surface = access.Surface(frame, frame.bounds, access.SCREEN, 0, 0)
        with patch.object(access, "extents", side_effect=lambda node, _: node.bounds):
            reading = access.pointer_reading(surface, (102, 65), lambda: None)
        self.assertEqual([u.text for u in reading.utterances()], ["Visible heading"])

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

    def test_ocr_can_capture_a_non_active_popup_on_a_scaled_monitor(self):
        request = {
            "screen": [-100, 0, 100, 80],
            "active": {"frame": [0, 0, 100, 80]},
            "hovered": {"frame": [-110, 5, 40, 20]},
        }
        mode, source, bounds = ocr.capture_region(request)
        self.assertEqual(mode, ["--current"])
        self.assertEqual(bounds, Rect(-100, 5, 30, 20))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.png"
            pixbuf = ocr.GdkPixbuf.Pixbuf.new(ocr.GdkPixbuf.Colorspace.RGB, False, 8, 200, 160)
            pixbuf.fill(0xFFFFFFFF)
            pixbuf.savev(str(path), "png", [], [])
            self.assertEqual(ocr.crop(path, source, bounds), (60, 40))

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
