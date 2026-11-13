"""F6-F10 carry the conventional script conversions (hiragana, katakana,
half-width katakana, full-/half-width alphanumerics); the learning
corrections live on Shift+F6/F7 (tests/test_learning.py)."""
from __future__ import annotations

import unittest

from oyayubi.chord.fsm import AmbShift, Plain
from oyayubi.ime.server import dispatch
from oyayubi.ime.session import ImeSession
from oyayubi.lattice.mozc_dict import load_lex
from oyayubi.lattice.table import load_table

VK_F6, VK_F7, VK_F8, VK_F9, VK_F10 = 0x75, 0x76, 0x77, 0x78, 0x79


def type_reading(s, reading):
    # Same shortcut as tests/test_learning.py: unambiguous faces, no FSM.
    s._table = {"keys": {c: {"plain": c} for c in reading}}
    s.tokens = [Plain(c) for c in reading]
    s._refresh()


def type_keys(s, keys):
    """Real NICOLA keys (unshifted faces), so F9/F10 have key letters to show."""
    s._table = load_table()
    s.tokens = [Plain(k) for k in keys]
    s._refresh()


def tap(s, vk):
    """A full press: a held key's repeat does not act again."""
    reply = dispatch(s, {"op": "down", "vk": vk})
    dispatch(s, {"op": "up", "vk": vk})
    return reply


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
        for vk in (VK_F6, VK_F7, VK_F8, VK_F9, VK_F10):
            reply = dispatch(s, {"op": "down", "vk": vk})
            self.assertEqual((reply["composition"], reply["commit"]), ("", ""))

    def test_f8_half_width_katakana(self):
        s = self.session()
        type_reading(s, "がっこう")
        reply = dispatch(s, {"op": "down", "vk": VK_F8})
        self.assertEqual(reply["composition"], "ｶﾞｯｺｳ")
        self.assertTrue(s.converted)
        reply = dispatch(s, {"op": "down", "vk": 13})
        self.assertEqual(reply["commit"], "ｶﾞｯｺｳ")

    def typed_cuamba(self):
        """クアンバ (Cuamba, a town in Mozambique) on the single thumb key:
        あ/じ share `s` and ば/み share `h`, left and right thumb faces, so
        the decoder may read くじんば."""
        s = self.session()
        s._table = load_table()
        s.tokens = [Plain("i"), AmbShift("s"), Plain(";"), AmbShift("h")]  # く あ ん ば
        s._refresh()
        return s

    def test_repeated_f7_reaches_cuamba(self):
        s = self.typed_cuamba()
        seen = [tap(s, VK_F7)["composition"] for _ in range(5)]
        # Each press is the next reading; the fifth wraps to the first.
        self.assertEqual(seen, ["クジンバ", "クジンミ", "クアンミ", "クアンバ", "クジンバ"])

    def test_repeated_f6_steps_through_the_readings_in_hiragana(self):
        s = self.typed_cuamba()
        seen = [tap(s, VK_F6)["composition"] for _ in range(4)]
        self.assertEqual(seen[0], "くじんば")  # the decoder's reading comes first
        self.assertEqual(sorted(seen), sorted(["くじんば", "くじんみ", "くあんみ", "くあんば"]))

    def test_f6_then_f7_starts_katakana_at_the_first_reading(self):
        s = self.typed_cuamba()
        tap(s, VK_F6)
        tap(s, VK_F6)  # step to the next reading
        self.assertEqual(tap(s, VK_F7)["composition"], "クジンバ")

    def test_unambiguous_repeat_stays(self):
        s = self.session()
        type_reading(s, "きょう")
        self.assertEqual(tap(s, VK_F6)["composition"], "きょう")
        self.assertEqual(tap(s, VK_F6)["composition"], "きょう")
        self.assertEqual(tap(s, VK_F7)["composition"], "キョウ")
        self.assertEqual(tap(s, VK_F7)["composition"], "キョウ")

    def test_f8_uses_the_selected_reading(self):
        s = self.typed_cuamba()
        for _ in range(4):
            if tap(s, VK_F7)["composition"] == "クアンバ":
                break
        self.assertEqual(tap(s, VK_F8)["composition"], "ｸｱﾝﾊﾞ")

    def test_f10_gives_the_typed_keys_and_cycles_case(self):
        s = self.session()
        type_keys(s, "ew")  # た か
        self.assertEqual("".join(t["kana"] for t in s._lattice()), "たか")
        surfaces = [tap(s, VK_F10)["composition"] for _ in range(4)]
        self.assertEqual(surfaces, ["ew", "EW", "Ew", "ew"])
        self.assertEqual(s.segments[s.focus].selected.reading, "たか")

    def test_f9_full_width_and_back_to_kana(self):
        s = self.session()
        type_keys(s, "ew")
        self.assertEqual(tap(s, VK_F9)["composition"], "ｅｗ")
        self.assertEqual(tap(s, VK_F9)["composition"], "ＥＷ")
        self.assertEqual(tap(s, VK_F10)["composition"], "ew")  # the other width starts over
        self.assertEqual(tap(s, VK_F6)["composition"], "たか")

    def test_alphanumerics_are_not_learned(self):
        import tempfile
        from pathlib import Path
        from oyayubi.ime.learning import LearningStore
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.jsonl"
            path.touch()
            s = ImeSession(_lex=self.lex, learning_path=None, _learning=LearningStore(path))
            type_keys(s, "ew")
            dispatch(s, {"op": "down", "vk": VK_F10})
            self.assertEqual(dispatch(s, {"op": "down", "vk": 13})["commit"], "ew")
            self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_shift_f8_to_f10_pass_through(self):
        s = self.session()
        type_reading(s, "きょう")
        before = s.composition
        for vk in (VK_F8, VK_F9, VK_F10):
            reply = dispatch(s, {"op": "down", "vk": vk, "mods": ["shift"]})
            self.assertEqual(reply["composition"], before)
            self.assertFalse(s.converted)


if __name__ == "__main__":
    unittest.main()
