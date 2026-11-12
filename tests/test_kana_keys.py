"""F6/F7 carry the conventional script conversions (hiragana/katakana);
the learning corrections live on Shift+F6/F7 (tests/test_learning.py)."""
from __future__ import annotations

import unittest

from oyayubi.chord.fsm import Plain
from oyayubi.ime.server import dispatch
from oyayubi.ime.session import ImeSession
from oyayubi.lattice.mozc_dict import load_lex

VK_F6, VK_F7 = 0x75, 0x76


def type_reading(s, reading):
    # Same shortcut as tests/test_learning.py: unambiguous faces, no FSM.
    s._table = {"keys": {c: {"plain": c} for c in reading}}
    s.tokens = [Plain(c) for c in reading]
    s._refresh()


class KanaKeyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lex = load_lex()

    def session(self):
        return ImeSession(_lex=self.lex, learning_path=None)

    def test_f7_converts_unconverted_input_to_katakana(self):
        s = self.session()
        type_reading(s, "きょう")
        reply = dispatch(s, {"op": "down", "vk": VK_F7})
        self.assertEqual(reply["composition"], "キョウ")
        self.assertTrue(s.converted)
        selected = s.segments[s.focus].selected
        self.assertEqual((selected.surface, selected.kind), ("キョウ", "kana"))

    def test_f6_selects_hiragana(self):
        s = self.session()
        type_reading(s, "きょう")
        dispatch(s, {"op": "down", "vk": VK_F7})
        self.assertEqual(s.composition, "キョウ")
        reply = dispatch(s, {"op": "down", "vk": VK_F6})
        self.assertEqual(reply["composition"], "きょう")
        self.assertEqual(s.segments[s.focus].selected.kind, "kana")

    def test_f7_on_converted_input(self):
        s = self.session()
        type_reading(s, "きょう")
        s.converted = True
        s._open_cands()
        self.assertNotEqual(s.composition, "キョウ")
        reply = dispatch(s, {"op": "down", "vk": VK_F7})
        self.assertEqual(reply["composition"], "キョウ")

    def test_kana_selection_commits(self):
        s = self.session()
        type_reading(s, "きょう")
        dispatch(s, {"op": "down", "vk": VK_F7})
        reply = dispatch(s, {"op": "down", "vk": 13})
        self.assertEqual(reply["commit"], "キョウ")

    def test_f_keys_without_input_are_noops(self):
        s = self.session()
        for vk in (VK_F6, VK_F7):
            reply = dispatch(s, {"op": "down", "vk": vk})
            self.assertEqual((reply["composition"], reply["commit"]), ("", ""))


if __name__ == "__main__":
    unittest.main()
