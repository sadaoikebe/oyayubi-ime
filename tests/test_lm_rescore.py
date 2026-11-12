"""Stage 5b: LM rescoring on Space (fake scorer; real-model checks need torch)."""
from __future__ import annotations

import importlib.util
import json
import os
import unittest

from oyayubi.chord.fsm import AmbShift, Plain, ThumbTap
from oyayubi.ime.lm_rescore import LmRescorer, conversion_picks
from oyayubi.ime.session import ImeSession
from oyayubi.lattice.decode import DecodeResult

KIDOKU = [Plain("k"), AmbShift("j"), Plain("i")]  # き + (ど|お) + く


class FakeScorer:
    """NLL 0 for preferred surfaces, 10 nats otherwise; records every call."""

    def __init__(self, preferred: set[str]):
        self.preferred = preferred
        self.calls: list[tuple[str, list[str]]] = []

    def nll(self, context, candidates):
        self.calls.append((context, list(candidates)))
        return [0.0 if c in self.preferred else 10.0 for c in candidates]


def result(surface: str, score: int) -> DecodeResult:
    return DecodeResult(surface, surface, score, [])


class ChooseTest(unittest.TestCase):
    def test_formula_and_ties(self):
        pool = [result("a", 100), result("b", 600), result("c", 700)]
        scorer = FakeScorer({"b", "c"})
        # a: 0 + 1*500*10 = 5000; b: 500 + 0; c: 600 + 0
        self.assertEqual(LmRescorer(scorer, alpha=1.0).choose("", pool).surface, "b")
        # alpha=0 never calls the LM and keeps the 1-best.
        scorer.calls.clear()
        self.assertEqual(LmRescorer(scorer, alpha=0).choose("", pool).surface, "a")
        self.assertEqual(scorer.calls, [])
        # Equal totals keep the earlier (1-best) candidate.
        tie = [result("a", 0), result("b", 0)]
        self.assertEqual(LmRescorer(FakeScorer(set()), alpha=1).choose("", tie).surface, "a")

    def test_single_candidate_skips_lm(self):
        scorer = FakeScorer(set())
        self.assertEqual(LmRescorer(scorer).choose("x", [result("a", 0)]).surface, "a")
        self.assertEqual(scorer.calls, [])


class SessionRescoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = ImeSession(learning_path=None)
        cls.s.load()

    def setUp(self):
        self.s.reset()
        self.s.lm = None

    def tearDown(self):
        self.s.lm = None
        self.s.reset()

    def type_and_convert(self, tokens):
        self.s.tokens = list(tokens)
        self.s._refresh()
        before = self.s.composition
        self.s._ingest([ThumbTap()])
        return before

    def test_space_applies_lm_pick_but_display_stays_1best(self):
        self.s.lm = LmRescorer(FakeScorer({"既読"}), alpha=1.0, k=8)
        before = self.type_and_convert(KIDOKU)
        self.assertEqual(before, "記憶")  # unconverted display: plain Viterbi 1-best
        self.assertTrue(self.s.converted)
        self.assertEqual(self.s.composition, "既読")
        self.assertEqual(self.s.candidates[self.s.cand_index], "既読")
        self.assertIn("記憶", self.s.candidates)
        self.assertEqual(self.s.lm.last["runs"], 1)
        self.assertEqual(self.s.lm.last["changed"], 1)
        # Backspace cancels conversion back to the Viterbi 1-best.
        self.s.on_key(True, 0x08, 0)
        self.assertFalse(self.s.converted)
        self.assertEqual(self.s.composition, "記憶")

    def test_flag_off_is_unchanged(self):
        self.type_and_convert(KIDOKU)
        self.assertEqual(self.s.composition, "記憶")

    def test_no_lm_call_while_typing(self):
        scorer = FakeScorer({"既読"})
        self.s.lm = LmRescorer(scorer)
        self.s.tokens = list(KIDOKU)
        self.s._refresh()
        self.s._ingest([Plain("h")])
        self.assertEqual(scorer.calls, [])

    def test_left_context_is_committed_text_and_resets(self):
        scorer = FakeScorer(set())
        self.s.lm = LmRescorer(scorer)
        self.type_and_convert(KIDOKU)
        self.assertEqual(scorer.calls[-1][0], "")
        committed = self.s._commit()  # Enter
        self.assertEqual(committed, "記憶")
        self.type_and_convert(KIDOKU)
        self.assertEqual(scorer.calls[-1][0], "記憶")
        # Typing after a conversion commits it and keeps accumulating context.
        self.s._ingest([Plain("k"), AmbShift("j"), Plain("i")])
        self.s._ingest([ThumbTap()])
        self.assertEqual(scorer.calls[-1][0], "記憶記憶")
        # Backspace on an empty composition edits the remembered context.
        self.s._commit()
        self.s.on_key(True, 0x08, 0)
        self.assertEqual(self.s._lm_context, "記憶記憶記")
        # Caret movement with no composition forgets the context.
        self.s.on_key(True, 0x25, 0)  # Left arrow
        self.assertEqual(self.s._lm_context, "")
        self.s._note_commit("前文")
        self.s.reset()  # reset op (focus loss)
        self.assertEqual(self.s._lm_context, "")
        self.s._note_commit("前文")
        self.s.set_direct(True)
        self.s.set_direct(False)
        self.assertEqual(self.s._lm_context, "")

    def test_context_includes_earlier_runs_of_the_same_composition(self):
        from oyayubi.ime.session import Literal
        scorer = FakeScorer(set())
        self.s.lm = LmRescorer(scorer)
        self.s._note_commit("前")
        self.type_and_convert(KIDOKU + [Literal("、")] + KIDOKU)
        contexts = [c for c, _ in scorer.calls]
        self.assertEqual(contexts, ["前", "前記憶、"])


HAVE_TORCH = all(importlib.util.find_spec(m) for m in ("torch", "transformers", "sentencepiece"))


@unittest.skipUnless(HAVE_TORCH and os.environ.get("OYAYUBI_LM_TESTS"),
                     "set OYAYUBI_LM_TESTS=1 with torch/transformers and the model cached")
class RealModelTest(unittest.TestCase):
    """The optimized scorer must give E6's (naive) numbers exactly."""

    @classmethod
    def setUpClass(cls):
        from oyayubi.ime.lm_rescore import CausalLmScorer
        cls.make = CausalLmScorer
        cls.ref = CausalLmScorer(kv_cache=False)

    def test_variants_match_naive(self):
        cands = ["記憶にない", "既読にない", "記憶に泣い", "既読", "きどくにない", "記"]
        variants = [self.make(), self.make(incremental=False)]
        for ctx in ("", "***REMOVED***", "***REMOVED***"):
            ref = self.ref.nll(ctx, cands)
            for v in variants:
                for a, b in zip(v.nll(ctx, cands), ref):
                    self.assertAlmostEqual(a, b, delta=1e-3)

    def test_conversion_picks_match_pinned_refresh(self):
        """Pinned before the refactor from _refresh(rescore=True) at 75703b8."""
        from tests.fixtures.make_lm_conversion_fixture import OUT, session_tokens
        from oyayubi.lattice.table import load_table, reverse_table
        rows = json.loads(OUT.read_text(encoding="utf-8"))
        reverse = reverse_table(load_table())
        sess = ImeSession(learning_path=None)
        sess.load()
        sess.lm = LmRescorer(self.make())
        for row in rows:
            expected = (row["composition"], row["segments"])
            with self.subTest(input=row["input"]):
                sess.reset()
                sess._lm_context = row["context"]
                sess.tokens = session_tokens(row["input"], reverse)
                sess._refresh()
                picks = conversion_picks(sess._parts(), sess._lex, None, sess._left_surface,
                                         sess._lm_context, LmRescorer(sess.lm.scorer))
                sess._refresh(picks=picks)  # the worker path
                actual = (sess.composition,
                          [[s.start, s.end, s.selected.surface] for s in sess.segments])
                self.assertEqual(actual, expected)
                sess._refresh()
                sess._refresh(rescore=True)  # the synchronous path
                actual = (sess.composition,
                          [[s.start, s.end, s.selected.surface] for s in sess.segments])
                self.assertEqual(actual, expected)

    def test_prime_does_not_change_nll(self):
        cands = ["記憶にない", "既読にない", "既読"]
        cold = self.make()
        primed = self.make()
        for ctx in ("***REMOVED***", "***REMOVED***"):
            primed.prime(ctx)
            for a, b in zip(primed.nll(ctx, cands), cold.nll(ctx, cands)):
                self.assertAlmostEqual(a, b, delta=1e-3)
        self.assertEqual(primed.stats["ctx_extend"], 1)  # the second prime extended the first

    def test_window_is_deterministic(self):
        long = "これは長い上文の例です。" * 30
        warm = self.make()
        for i in range(0, len(long), 7):
            warm.nll(long[:i], ["既読"])
        cold = self.make()
        self.assertAlmostEqual(warm.nll(long, ["既読", "記憶"])[1],
                               cold.nll(long, ["既読", "記憶"])[1], delta=1e-3)


if __name__ == "__main__":
    unittest.main()
