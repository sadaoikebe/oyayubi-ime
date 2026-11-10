from __future__ import annotations

from itertools import product
import random
import unittest

from tests.test_bunsetsu import lexicon
from tools.wp0.mozc_dict import UNK_COST
from tools.wp0.run_ajimee_nbest_eval import chunk_reaches, sentence_candidates
from tools.wp0.token_viterbi import _spans_from, nbest_tokens


def plain(text):
    return {"kind": "plain", "kana": text}


def exhaustive(tokens, lex):
    """Enumerate every path; score kana runs independently across literals."""
    paths = {}

    def walk(pos, rid, score, surface, reading):
        if pos == len(tokens):
            total = score + (lex.transition(rid, 0) if rid is not None else 0)
            old, readings = paths.get(surface, (float("inf"), set()))
            if total < old:
                paths[surface] = (total, {reading})
            elif total == old:
                readings.add(reading)
            return
        for w in _spans_from(tokens, pos, lex):
            if w.unknown or w.entry.cost >= UNK_COST:
                continue
            e = w.entry
            if w.literal:
                cost = lex.transition(rid, 0) if rid is not None else 0
                nxt = None
            else:
                cost = lex.transition(0 if rid is None else rid, e.lid) + e.cost
                nxt = e.rid
            walk(w.end, nxt, score + cost, surface + e.surface, reading + e.reading)

    walk(0, None, 0, "", "")
    return paths


class NbestTest(unittest.TestCase):
    def assert_exact(self, tokens, lex):
        expected = exhaustive(tokens, lex)
        actual, stats = nbest_tokens(tokens, lex, k=len(expected) + 10)
        self.assertEqual({d.surface: d.score for d in actual},
                         {s: cost for s, (cost, _) in expected.items()})
        self.assertEqual(len(actual), len(expected))
        self.assertEqual([d.score for d in actual], sorted(d.score for d in actual))
        self.assertEqual((stats["n_tokens"], stats["nbest"]), (len(tokens), len(expected)))
        for d in actual:
            self.assertIn(d.reading, expected[d.surface][1])
            self.assertEqual("".join(p.surface for p in d.pieces), d.surface)
            self.assertEqual("".join(p.reading for p in d.pieces), d.reading)
            self.assertEqual([p.start for p in d.pieces], [0] + [p.end for p in d.pieces[:-1]]
                             if d.pieces else [])
            self.assertEqual(d.pieces[-1].end if d.pieces else 0, len(tokens))
            self.assertFalse(any(p.learned for p in d.pieces))
        for k in (1, 3, 8, 30):
            prefix, _ = nbest_tokens(tokens, lex, k=k)
            self.assertEqual(prefix, actual[:k])

    def test_internal_alternatives_shared_rid_and_unbounded_score_gap(self):
        # All alternatives merge before an identical tail. Reading/RID beams
        # or tracing one path per terminal lose these internal alternatives.
        lex = lexicon([("か", f"候補{i}", 1, 1, i * 400) for i in range(25)]
                      + [("く", "区", 1, 1, 20), ("く", "句", 1, 1, 80)],
                      [[77, 13], [19, -30]])
        self.assert_exact([plain("か"), plain("く")], lex)
        # A costly prefix outside a 12-RID beam becomes globally optimal via
        # its next connection. The exact forward potential must keep its RID.
        matrix = [[0] * 22 for _ in range(22)]
        matrix[20][21] = -5000
        lex = lexicon([("か", f"別POS{i}", i, i, i) for i in range(1, 21)]
                      + [("く", "区", 21, 21, 0)], matrix)
        self.assert_exact([plain("か"), plain("く")], lex)
        self.assertEqual(nbest_tokens([plain("か"), plain("く")], lex, k=1)[0][0].surface,
                         "別POS20区")

    def test_full_lattice_negative_costs_duplicate_surfaces_and_boundaries(self):
        entries = [("か", "蚊", 1, 2, 50), ("か", "科", 1, 2, 80),
                   ("か", "科", 2, 3, -10), ("が", "科", 3, 1, -300),
                   ("く", "区", 0, 0, 20), ("ぐ", "具", 1, 3, 200),
                   ("かく", "書く", 2, 3, 90), ("がく", "学", 3, 1, 500),
                   ("がぐ", "科具", 3, 1, 8000), ("ぉ", "尾", 2, 2, 100),
                   ("かぉく", "顔奥", 1, 1, -900),
                   ("？", "不可", 1, 1, -90000),
                   ("か？く", "不可", 1, 1, -90000)]
        lex = lexicon(entries, [[77, 300, -50, 30], [500, 0, 70, -500],
                                [-100, 600, -40, 90], [200, -300, 500, 40]])
        amb = {"kind": "amb", "faces": {"left": "か", "right": "が"}}
        mixed = {"kind": "amb", "faces": {"left": "？", "right": "ぉ"}}
        for ts in ([amb, {"kind": "amb", "faces": {"left": "く", "right": "ぐ"}}],
                   [amb, mixed, plain("く")],
                   [mixed, mixed],
                   [{"kind": "literal", "text": "！"}, amb,
                    {"kind": "literal", "text": "、"},
                    {"kind": "literal", "text": "。"}, plain("く")]):
            with self.subTest(tokens=ts):
                self.assert_exact(ts, lex)

    def test_random_tiny_lattices_against_exhaustive_oracle(self):
        rng = random.Random(502)
        for trial in range(30):
            matrix = [[rng.randrange(-500, 600) for _ in range(4)] for _ in range(4)]
            entries = [(r, rng.choice(("甲", "乙", r)), rng.randrange(4),
                        rng.randrange(4), rng.randrange(-400, 800))
                       for r in ("か", "が", "く", "ぐ", "かく", "がく", "ぉ")
                       for _ in range(3)]
            lex = lexicon(entries, matrix)
            ts = [{"kind": "amb", "faces": {"left": "か", "right": "が"}},
                  {"kind": "amb", "faces": {"left": "？", "right": "ぉ"}},
                  {"kind": "amb", "faces": {"left": "く", "right": "ぐ"}}]
            with self.subTest(trial=trial):
                self.assert_exact(ts, lex)

    def test_empty_k_and_existing_unknown_high_cost_policy(self):
        lex = lexicon([("か", "不可", 1, 1, UNK_COST)], [[77, 0], [0, 0]])
        self.assertEqual(nbest_tokens([plain("か")], lex)[0], [])
        self.assertEqual(nbest_tokens([plain("が")], lex)[0], [])
        for k in (0, -1):
            out, stats = nbest_tokens([], lex, k=k)
            self.assertEqual(out, [])
            self.assertEqual(stats["nbest"], 0)
        out, _ = nbest_tokens([], lex)
        self.assertEqual([(d.surface, d.reading, d.score, d.pieces) for d in out],
                         [("", "", 0, [])])


class NbestEvaluationTest(unittest.TestCase):
    def test_sentence_ranks_and_chunk_oracle_are_different(self):
        cs = [[("甲", -5), ("乙", -4)], [("、", 0)], [("丙", 10), ("丁", 20)]]
        self.assertEqual(sentence_candidates(cs, 3), ["甲、丙", "乙、丙", "甲、丁"])
        self.assertTrue(chunk_reaches(cs, "乙、丁", 2))
        self.assertNotIn("乙、丁", sentence_candidates(cs, 2))
        self.assertFalse(chunk_reaches(cs, "乙丁", 2))
        self.assertFalse(chunk_reaches(cs, "乙、丁。", 2))

    def test_sentence_merge_matches_cartesian_oracle_and_deduplicates(self):
        cs = [[("", -2), ("か", 1), ("かか", 9)], [("か", -1), ("", 3)]]
        exhaustive = {}
        for path in product(*cs):
            surface = "".join(s for s, _ in path)
            score = sum(c for _, c in path)
            exhaustive[surface] = min(exhaustive.get(surface, float("inf")), score)
        ordered = sorted(exhaustive, key=exhaustive.get)
        self.assertEqual(sentence_candidates(cs, 100), ordered)
        self.assertEqual(sentence_candidates([[], [("。", 0)]], 8), [])
        self.assertFalse(chunk_reaches([[], [("。", 0)]], "。", 8))
        self.assertEqual(sentence_candidates([], 8), [""])
        self.assertTrue(chunk_reaches([], "", 8))


if __name__ == "__main__":
    unittest.main()
