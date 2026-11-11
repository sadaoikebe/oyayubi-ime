from __future__ import annotations

import unittest

from oyayubi.ime.bunsetsu import Candidate, CandidateStream, candidates, segments_from
from tests.test_bunsetsu import lexicon
from tools.wp0.expand import is_literal_face
from tools.wp0.run_mozc_eval import reverse_table
from tools.wp0.table import load_table
from tools.wp0.token_viterbi import decode_tokens, nbest_tokens


def plain(text):
    return {"kind": "plain", "kana": text}


def mixed(literal="？"):
    return {"kind": "amb", "faces": {"left": literal, "right": "ぉ"}}


class BracketToutenTest(unittest.TestCase):
    def test_bracket_key_is_the_firmware_touten(self):
        # The author's QMK table sends KC_COMM from both P and LBRC; the
        # host IME rendered it as 、. Here the IME itself must do that
        # rendering, so "[" carries the same literal plain face as "p".
        from oyayubi.chord.fsm import Plain
        from oyayubi.chord.keys import VK_OEM_4, classify
        from oyayubi.ime.session import to_lattice_token

        self.assertEqual(classify(VK_OEM_4), ("M", "["))
        table = load_table()
        self.assertEqual(to_lattice_token(Plain("["), table),
                         {"kind": "literal", "text": "、"})
        self.assertEqual(to_lattice_token(Plain("p"), table),
                         {"kind": "literal", "text": "、"})


class LiteralFacesTest(unittest.TestCase):
    def test_literal_alphabet_matches_stage2a(self):
        for text in "ぁあぉゔゖー":
            self.assertFalse(is_literal_face(text), text)
        for text in "？。，、．・!?1AZ゛゜゙゚ア":
            self.assertTrue(is_literal_face(text), text)
        reverse = reverse_table(load_table())
        self.assertNotIn("？", reverse)  # Reading evaluation coverage stays kana-only.
        self.assertEqual(reverse["ぉ"]["faces"], {"left": "？", "right": "ぉ"})

    def test_decode_and_nbest_use_independent_runs(self):
        # BOS->BOS is deliberately nonzero. Neither empty runs nor consecutive
        # literals should add it. Symbol dictionary entries must never be used.
        lex = lexicon([
            ("か", "蚊", 1, 1, 100), ("か", "科", 2, 2, 110),
            ("く", "区", 1, 1, 120), ("ぉ", "尾", 2, 2, 1000),
            ("かぉく", "顔奥", 1, 1, 500),
            ("？", "記号変換不可", 1, 1, -90000),
            ("か？く", "境界越え不可", 1, 1, -90000),
        ], [[77, 13, 17], [19, -900, 23], [29, 31, -700]])
        tokens = [plain("か"), mixed(), plain("く")]
        best, _ = decode_tokens(tokens, lex)
        before, _ = decode_tokens([plain("か")], lex)
        after, _ = decode_tokens([plain("く")], lex)
        self.assertEqual((best.surface, best.reading, best.score),
                         ("蚊？区", "か？く", before.score + after.score))
        self.assertEqual([(p.start, p.end, p.literal) for p in best.pieces],
                         [(0, 1, False), (1, 2, True), (2, 3, False)])
        segments = segments_from(best.pieces, 0)
        self.assertEqual([(s.start, s.end, s.literal) for s in segments],
                         [(0, 1, False), (1, 2, False), (2, 3, False)])
        self.assertEqual(segments[1].selected.kind, "literal")
        results, _ = nbest_tokens(tokens, lex, k=100)
        by_surface = {r.surface: r for r in results}
        self.assertEqual(by_surface["蚊？区"].score, best.score)
        self.assertEqual(by_surface["顔奥"].reading, "かぉく")
        self.assertEqual(by_surface["顔奥"].score, 532)
        self.assertFalse(any("不可" in r.surface for r in results))
        for text in "？。，、．・!?1AZ゛゜゙゚ア":
            with self.subTest(text=text):
                literal = {"kind": "literal", "text": text}
                direct, _ = decode_tokens([literal, literal], lex)
                explicit, _ = nbest_tokens([literal, literal], lex)
                normalized = "、" if text == "，" else text
                self.assertEqual((direct.surface, direct.score), (text * 2, 0))
                self.assertEqual([(r.surface, r.score) for r in explicit],
                                 [(text * 2, 0)])
                amb, _ = decode_tokens([mixed(text), mixed(text)], lex)
                self.assertEqual((amb.surface, amb.score), (normalized * 2, 0))

    def test_candidate_scores_reset_both_sides_for_every_neighbor(self):
        matrix = [[77, 13, 17], [19, -900, 23], [29, 31, -700]]
        lex = lexicon([("ぉ", "尾", 2, 2, 1000)], matrix)
        for left in range(3):
            for right in range(3):
                with self.subTest(left=left, right=right):
                    cs = list(candidates([mixed()], lex, left, right))
                    self.assertEqual({c.surface for c in cs[:2]}, {"？", "尾"})
                    self.assertEqual([c.score for c in cs[:2]], sorted(c.score for c in cs[:2]))
                    self.assertEqual([c.surface for c in cs[2:]], ["ぉ", "ォ"])
                    literal = next(c for c in cs if c.kind == "literal")
                    word = next(c for c in cs if c.kind == "dictionary")
                    hira, kata = cs[2:]
                    self.assertEqual((literal.kind, literal.lid, literal.rid, literal.score),
                                     ("literal", 0, 0,
                                      (matrix[left][0] if left else 0)
                                      + (matrix[0][right] if right else 0)))
                    self.assertEqual([c.score for c in (word, hira, kata)],
                                     [matrix[left][2] + 1000 + matrix[2][right]] * 3)

    def test_literal_escape_outside_window_and_mixed_kana_fallback(self):
        lex = lexicon([("ぉ", "尾", 1, 1, -10000)], [[0, 0], [0, 0]])
        cs = list(candidates([mixed()], lex, 0, 0))
        self.assertEqual([(c.surface, c.kind) for c in cs],
                         [("尾", "dictionary"), ("ぉ", "kana"), ("ォ", "kana"),
                          ("？", "literal")])
        best, _ = decode_tokens([mixed()], lex)
        self.assertEqual(best.surface, "尾")
        results, _ = nbest_tokens([mixed()], lex)
        self.assertEqual([r.surface for r in results], ["尾", "？"])
        # A dictionary spelling can coincide with the literal, but revisiting
        # the literal selection must retain its boundary semantics and reading.
        same = lexicon([("ぉ", "？", 1, 1, -10000)], [[0, 0], [0, 0]])
        stream = CandidateStream(candidates([mixed()], same, 0, 0))
        self.assertEqual(stream.find(Candidate("？", "？", 0, 0, 0, "literal")), 3)
        # Conversely, a cheap literal must not set the dictionary band and
        # eliminate a valid, expensive conversion on the other face.
        expensive = lexicon([("ぉ", "尾", 1, 1, 15000)], [[0, 0], [0, 0]])
        self.assertEqual([(c.surface, c.kind) for c in candidates([mixed()], expensive, 0, 0)],
                         [("？", "literal"), ("尾", "dictionary"),
                          ("ぉ", "kana"), ("ォ", "kana")])
        # Kana fallback has to cover mixed paths even with no dictionary prefix.
        empty = lexicon([], [[0, 0], [0, 0]])
        cs = list(candidates([plain("か"), mixed("ア"), plain("く")], empty, 0, 0))
        self.assertEqual([(c.surface, c.reading) for c in cs],
                         [("かアく", "かアく"), ("カアク", "かアく"),
                          ("かぉく", "かぉく"), ("カォク", "かぉく")])


if __name__ == "__main__":
    unittest.main()
