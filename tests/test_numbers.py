"""Number spellings and the selectable number segment (U2)."""
from __future__ import annotations

import unittest

from oyayubi.ime.numbers import number_spellings, positional_kanji
from oyayubi.ime.session import ImeSession

VK_SPACE, VK_RIGHT, VK_LEFT, VK_DOWN, VK_RETURN = 0x20, 0x27, 0x25, 0x28, 0x0D


class SpellingTest(unittest.TestCase):
    def test_positional_kanji(self):
        cases = {"0": "〇", "3": "三", "10": "十", "11": "十一", "20": "二十",
                 "105": "百五", "1234": "千二百三十四", "10000": "一万",
                 "20261112": "二千二十六万千百十二", "100010001": "一億一万一",
                 "300000000000": "三千億"}
        for digits, expected in cases.items():
            self.assertEqual(positional_kanji(digits), expected, digits)
        self.assertIsNone(positional_kanji("007"))
        self.assertIsNone(positional_kanji("1" + "0" * 20))  # past 京

    def test_rows(self):
        self.assertEqual(number_spellings("3"), ["3", "３", "三"])
        self.assertEqual(number_spellings("1234"),
                         ["1234", "１２３４", "一二三四", "千二百三十四", "1,234", "１，２３４"])
        self.assertEqual(number_spellings("007"), ["007", "００７", "〇〇七"])
        self.assertEqual(number_spellings("123")[0], "123")  # typed digits stay first
        self.assertNotIn("1,23", number_spellings("123"))


class NumberSegmentTest(unittest.TestCase):
    """A run of typed digits is one selectable segment (session wiring)."""

    @classmethod
    def setUpClass(cls):
        cls.base = ImeSession(learning_path=None)
        cls.base.load()

    def session(self):
        s = ImeSession(learning_path=None)
        s._table, s._lex = self.base._table, self.base._lex
        return s

    def press(self, s, vk, t):
        out = s.on_key(True, vk, t) or ""
        return out + (s.on_key(False, vk, t + 20) or "")

    def type_digits(self, s, digits, t=0):
        for i, d in enumerate(digits):
            self.press(s, ord(d), t + 100 * i)

    def test_digits_alone(self):
        s = self.session()
        self.type_digits(s, "1234")
        self.assertEqual(s.composition, "1234")
        self.assertEqual([(g["start"], g["end"]) for g in s.segment_snapshot()], [(0, 4)])
        self.press(s, VK_SPACE, 1000)
        self.assertTrue(s.converted)
        self.assertEqual(s.candidates, number_spellings("1234"))
        self.assertEqual(s.candidate_kinds[0], "number")
        self.press(s, VK_DOWN, 1100)
        self.press(s, VK_DOWN, 1200)
        self.assertEqual(s.composition, "一二三四")
        self.assertEqual(self.press(s, VK_RETURN, 1300), "一二三四")

    def test_digits_then_kana(self):
        s = self.session()
        self.type_digits(s, "12")
        self.press(s, 0x57, 300)  # w: plain face
        self.assertTrue(s.composition.startswith("12"))
        self.press(s, VK_SPACE, 1000)
        # With kana present, ←→ still skip every literal, digits included.
        kana = s.focus
        self.assertFalse(s.segments[kana].literal)
        self.press(s, VK_LEFT, 1100)
        self.assertEqual(s.focus, kana)
        self.assertNotIn("１２", s.candidates)

    def test_digit_run_cannot_be_resized(self):
        s = self.session()
        self.type_digits(s, "12")
        self.press(s, VK_SPACE, 1000)
        s.on_key(True, 0x10, 1300)
        s.on_key(True, VK_LEFT, 1310)
        s.on_key(False, VK_LEFT, 1320)
        s.on_key(False, 0x10, 1330)
        self.assertEqual([(g["start"], g["end"]) for g in s.segment_snapshot()], [(0, 2)])

    def test_punctuation_is_still_a_fixed_literal(self):
        from oyayubi.ime.session import Literal
        s = self.session()
        s.tokens = [Literal("1"), Literal("0"), Literal("、"), Literal("2")]
        s._refresh()
        self.assertEqual([(g.selected.kind, g.selected.surface, g.start, g.end)
                          for g in s.segments],
                         [("number", "10", 0, 2), ("literal", "、", 2, 3),
                          ("number", "2", 3, 4)])
        self.assertEqual(s.focus, 0)

if __name__ == "__main__":
    unittest.main()
