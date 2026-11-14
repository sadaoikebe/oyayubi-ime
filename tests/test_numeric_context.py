"""A kana run right after a digit literal is scored as following a numeral (U1)."""
from __future__ import annotations

import unittest

from yuragi.ime.lm_rescore import LmRescorer, conversion_picks
from yuragi.ime.session import ImeSession, Literal
from yuragi.lattice.mozc_dict import BOS_ID, NUMBER_ID, MozcLex, left_id_after, load_lex
from yuragi.lattice.table import load_table, reverse_table
from yuragi.lattice.token_viterbi import decode_tokens, nbest_tokens

# Counters that the sentence-initial context got wrong (BOS: 際, お, 引き, おく).
COUNTERS = {"さい": "歳", "ど": "度", "ひき": "匹", "おく": "億", "せんえん": "千円"}


class LeftIdAfterTest(unittest.TestCase):
    def test_digits_only(self):
        lex = MozcLex({}, set(), [], NUMBER_ID + 1)
        self.assertEqual(left_id_after("3", lex), NUMBER_ID)
        self.assertEqual(left_id_after("３", lex), NUMBER_ID)
        for text in ("、", "。", "A", "!", ""):
            self.assertEqual(left_id_after(text, lex), BOS_ID, text)
        # A small test lexicon has no Mozc numeral POS.
        self.assertEqual(left_id_after("3", MozcLex({}, set(), [], 13)), BOS_ID)


class NumericContextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lex = load_lex()
        cls.table = load_table()
        cls.reverse = reverse_table(cls.table)

    def tokens(self, reading):
        return [self.reverse[c] for c in reading]

    def test_decode_and_nbest_after_digit(self):
        for reading, expected in COUNTERS.items():
            with self.subTest(reading):
                bos, _ = decode_tokens(self.tokens(reading), self.lex)
                self.assertNotEqual(bos.surface, expected)  # the old context
                after, _ = decode_tokens(self.tokens(reading), self.lex, left_id=NUMBER_ID)
                self.assertEqual(after.surface, expected)
                nb, _ = nbest_tokens(self.tokens(reading), self.lex, left_id=NUMBER_ID)
                self.assertEqual(nb[0].surface, expected)

    def test_default_left_id_is_unchanged(self):
        tokens = self.tokens("さい")
        self.assertEqual(decode_tokens(tokens, self.lex)[0].surface,
                         decode_tokens(tokens, self.lex, left_id=BOS_ID)[0].surface)
        self.assertEqual([d.surface for d in nbest_tokens(tokens, self.lex)[0]],
                         [d.surface for d in nbest_tokens(tokens, self.lex,
                                                          left_id=BOS_ID)[0]])

    def session(self, literal, reading):
        s = ImeSession(learning_path=None)
        s._table, s._lex = self.table, self.lex
        s.tokens = [Literal(literal)] + [self._token(c) for c in reading]
        s._refresh()
        return s

    def _token(self, kana):
        from yuragi.chord.fsm import AmbShift, Plain
        t = self.reverse[kana]
        return Plain(t["key"]) if t["kind"] == "plain" else AmbShift(t["key"])

    def test_session_composition_and_candidates(self):
        s = self.session("3", "さい")
        self.assertEqual(s.composition, "3歳")
        s.converted = True
        s._open_cands()
        self.assertEqual(s.candidates[0], "歳")
        # Punctuation is still a sentence boundary.
        self.assertEqual(self.session("、", "さい").composition,
                         "、" + decode_tokens(self.tokens("さい"), self.lex)[0].surface)

    def test_lm_picks_use_the_same_context(self):
        s = self.session("3", "ひき")
        picks = conversion_picks(s._parts(), self.lex, None, "", "",
                                 LmRescorer(scorer=None, alpha=0))
        self.assertEqual([p.surface for p in picks], ["匹"])


if __name__ == "__main__":
    unittest.main()
