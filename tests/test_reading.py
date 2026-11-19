from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reader import access, ocr, settings
from reader.model import Piece, Rect, from_word, passages


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

    def test_ocr_uses_target_and_keeps_screen_coordinates(self):
        bounds = Rect(300, 150, 650, 350)
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        rows = ("5\t1\t1\t1\t1\t30\t40\t50\t20\t97\tFirst\n"
                "5\t1\t1\t1\t1\t90\t40\t70\t20\t91\tsecond.\n")
        words = ocr.parse(header + rows, (1300, 700), bounds)
        self.assertEqual(ocr.at_point(words, (350, 173)), words[1:])
        self.assertEqual(ocr.at_point(words, (300, 150)), [])
        self.assertEqual(words[1].box, Rect(345, 170, 35, 10))

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
