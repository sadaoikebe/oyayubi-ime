from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oyayubi.chord.fsm import AmbShift, Plain
from oyayubi.ime.bunsetsu import COST_WINDOW, Candidate, candidates
from oyayubi.ime.learning import (CONTEXT_BONUS, HALF_LIFE_SECONDS, PAIR_BONUS,
                                  Confirmation, LearningStore)
from oyayubi.ime.server import dispatch
from oyayubi.ime.session import ImeSession
from tests.test_bunsetsu import lexicon
from tools.wp0.mozc_dict import Entry, MozcLex, load_lex
from tools.wp0.run_mozc_eval import reverse_table
from tools.wp0.token_viterbi import _end_cost, _spans_from, _word_cost, decode_tokens, nbest_tokens


def tokens(reading):
    return [{"kind": "plain", "kana": c} for c in reading]


class LearningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lex = load_lex()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "learning.jsonl"
        self.path.touch()
        self.now = 1_800_000_000.0

    def store(self):
        return LearningStore(self.path, clock=lambda: self.now)

    def session(self, *, lex=None):
        return ImeSession(_lex=lex or self.lex, learning_path=None, _learning=self.store())

    def type_reading(self, s, reading):
        # Exercise production refresh/candidate/commit logic without duplicating
        # the chord FSM tests. Every character has an unambiguous physical face.
        s._table = {"keys": {c: {"plain": c} for c in reading}}
        s.tokens = [Plain(c) for c in reading]
        s._refresh()

    def choose(self, s, surface, *, kind="dictionary", reading=None):
        if reading is not None:
            s.focus = next(i for i, seg in enumerate(s.segments)
                           if seg.selected.reading == reading)
        s.converted = True
        s._open_cands()
        stream = s.segments[s.focus].choices
        stream.last()
        index = next(i for i, c in enumerate(stream.cache)
                     if c.surface == surface and c.kind == kind)
        s._select(index)

    def rows(self):
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def test_kousei_confirmation_changes_best_and_candidate_order_then_delete(self):
        s = self.session()
        self.type_reading(s, "こうせい")
        self.assertEqual(s.composition, "構成")
        self.choose(s, "校正")
        self.assertEqual(dispatch(s, {"op": "down", "vk": 13})["commit"], "校正")
        self.assertEqual([(r["reading"], r["surface"], r["left"]) for r in self.rows()],
                         [("こうせい", "校正", "")])

        # A new session loads the persisted file, with the same BOS context.
        s = self.session()
        self.type_reading(s, "こうせい")
        self.assertEqual(s.composition, "校正")
        s.converted = True
        s._open_cands()
        self.assertEqual(s.candidates[0], "校正")
        # The same base lexicon is still used by offline 1-best and nbest.
        self.assertEqual(decode_tokens(tokens("こうせい"), self.lex)[0].surface, "構成")
        self.assertEqual(nbest_tokens(tokens("こうせい"), self.lex)[0][0].surface, "構成")

        self.path.unlink()
        s.reset()
        self.type_reading(s, "こうせい")
        self.assertEqual(s.composition, "構成")
        s.converted = True
        s._open_cands()
        self.assertEqual(s.candidates[0], "構成")
        dispatch(s, {"op": "down", "vk": 13})
        self.assertFalse(self.path.exists())  # no automatic recreation on commit

    def test_multiword_bunsetsu_records_left_and_preserves_raw_cost(self):
        s = self.session()
        self.type_reading(s, "きかんのこうせいをした")
        self.assertEqual(s.composition, "機関の構成をした")
        self.choose(s, "校正を", reading="こうせいを")
        raw_cost = s.segments[s.focus].selected.word_cost
        self.assertEqual(dispatch(s, {"op": "down", "vk": 13})["commit"], "機関の校正をした")
        self.assertEqual([(r["reading"], r["surface"], r["left"]) for r in self.rows()], [
            ("きかんの", "機関の", ""), ("こうせいを", "校正を", "機関の"),
            ("した", "した", "校正を")])
        s = self.session()
        self.type_reading(s, "きかんのこうせいをした")
        self.assertEqual(s.composition, "機関の校正をした")
        self.choose(s, "校正を", reading="こうせいを")
        self.assertEqual(s.candidates[0], "校正を")
        self.assertEqual(s.segments[s.focus].selected.word_cost, raw_cost)
        dispatch(s, {"op": "down", "vk": 13})
        self.assertEqual([r["cost"] for r in self.rows() if r["surface"] == "校正を"],
                         [raw_cost, raw_cost])

    def test_real_nicola_faces_and_session_load_enable_learning(self):
        def real_session():
            s = ImeSession(_lex=self.lex, learning_path=self.path)
            s.load()
            reverse = reverse_table(s._table)
            s.tokens = [(Plain if reverse[c]["kind"] == "plain" else AmbShift)(reverse[c]["key"])
                        for c in "きかんのこうせいをした"]
            s._refresh()
            return s

        s = real_session()
        self.assertEqual(s.composition, "機関の構成をした")
        self.choose(s, "校正を", reading="こうせいを")
        dispatch(s, {"op": "down", "vk": 13})
        s = real_session()
        self.assertEqual(s.composition, "機関の校正をした")
        self.assertIsNotNone(s._learning)
        s.learning_path = None
        s._learning = None
        s.load()
        s._refresh()
        self.assertEqual(s.composition, "機関の構成をした")
        self.assertIsNone(s._learning)

    def test_unknown_kana_becomes_dictionary_entry_and_deletion_removes_it(self):
        # The real dictionary can compose both spellings from shorter words.
        # Remove its word entries, while keeping its real POS connections, to
        # prove that a previously unavailable dictionary path is registered.
        lex = MozcLex({}, set(), self.lex.connect, self.lex.pos_size)
        for surface in ("ずんだもん", "ズンダモン"):
            with self.subTest(surface=surface):
                self.path.write_text("", encoding="utf-8")
                self.assertFalse(any(e.surface == surface
                                     for e in self.lex.by_reading.get("ずんだもん", ())))
                s = self.session(lex=lex)
                self.type_reading(s, "ずんだもん")
                self.choose(s, surface, kind="kana")
                self.assertEqual(dispatch(s, {"op": "down", "vk": 13})["commit"], surface)
                self.assertTrue(self.rows()[0]["user_word"])
                s = self.session(lex=lex)
                self.type_reading(s, "ずんだもん")
                cs = list(candidates(tokens("ずんだもん"), lex, 0, 0,
                                     learning=s._learning.snapshot()))
                self.assertIn((surface, "dictionary"), [(c.surface, c.kind) for c in cs])
                self.assertEqual(s.composition, surface)
                self.path.unlink()
                self.type_reading(s, "ずんだもん")
                cs = list(candidates(tokens("ずんだもん"), lex, 0, 0,
                                     learning=s._learning.snapshot()))
                self.assertNotIn((surface, "dictionary"), [(c.surface, c.kind) for c in cs])

    def test_context_bonus_flips_best_and_candidate_order(self):
        lex = lexicon([("か", "甲", 1, 1, 0), ("か", "乙", 1, 1, 4000)], [[0, 0], [0, 0]])
        store = self.store()
        store.confirm([(Candidate("乙", "か", 4000, 1, 1, word_cost=4000), "左")], lex)
        for left, expected in [("左", "乙"), ("別", "甲")]:
            model = store.snapshot()
            self.assertEqual(decode_tokens(tokens("か"), lex, learning=model,
                                           left_surface=left)[0].surface, expected)
            self.assertEqual(next(candidates(tokens("か"), lex, 0, 0, learning=model,
                                             left_surface=left)).surface, expected)

        # A selectable literal face also starts a fresh bunsetsu for following
        # non-content words; it must not become part of their spelling context.
        by = {"の": [Entry("の", "の", 433, 433, 0)],
              "か": [Entry("甲", "か", 1851, 1851, 0), Entry("乙", "か", 1851, 1851, 4000)]}
        lex = MozcLex(by, {"の", "か"}, self.lex.connect, self.lex.pos_size)
        store.confirm([(Candidate("乙", "か", 4000, 1851, 1851, word_cost=4000), "の")], lex)
        ts = [{"kind": "amb", "faces": {"left": "？", "right": "ぉ"}}] + tokens("のか")
        self.assertEqual(decode_tokens(ts, lex, learning=store.snapshot())[0].surface, "？の乙")
        self.assertEqual(next(candidates(tokens("か"), lex, 433, 0,
                                         learning=store.snapshot(), left_surface="の")).surface, "乙")

        # The fixed clock gives every confirmation the same timestamp. File
        # order must pick the latest POS even when an existing triple is updated.
        for left, pos in [("左", 1), ("の", 1851)]:
            store.confirm([(Candidate("乙", "か", 4000, pos, pos, word_cost=4000), left)], lex)
            for model in (store.snapshot(), self.store().snapshot()):
                self.assertEqual([(e.lid, e.rid) for _, e in model.spans(tokens("か"), 0, lex)],
                                 [(pos, pos)])
        self.assertEqual(decode_tokens(ts, lex, learning=store.snapshot())[0].surface, "？の乙")

    def test_long_unknown_and_history_reload_in_an_existing_session(self):
        lex = MozcLex({}, set(), self.lex.connect, self.lex.pos_size)
        reading = "ずんだもん" * 4  # longer than the base dictionary's 16-token limit
        s, other = self.session(lex=lex), self.session(lex=lex)
        self.type_reading(s, reading)
        self.choose(s, reading, kind="kana")
        dispatch(s, {"op": "down", "vk": 13})
        self.type_reading(other, reading)
        self.assertEqual(len(other.segments), 1)
        self.assertEqual(other.composition, reading)
        other.converted = True
        other._open_cands()
        self.assertEqual((other.candidates[0], other.candidate_kinds[0]), (reading, "dictionary"))

    def test_decay_recency_and_no_frequency_accumulation(self):
        store = self.store()
        c = Candidate("校正", "こうせい", 0, 1, 1, word_cost=5000)
        store.confirm([(c, "左")] * 100, self.lex)
        self.assertEqual(store.bonus(c.reading, c.surface, "左"), PAIR_BONUS + CONTEXT_BONUS)
        self.assertEqual(store.bonus(c.reading, c.surface, "別"), PAIR_BONUS)
        snapshot = store.snapshot()
        self.now += HALF_LIFE_SECONDS
        store.sync()
        self.assertEqual(snapshot.bonus(c.reading, c.surface, "左"), 6000)
        self.assertEqual(store.bonus(c.reading, c.surface, "左"), 3000)
        self.assertEqual(store.bonus(c.reading, c.surface, "別"), 1200)
        store.confirm([(c, "別")], self.lex)
        self.assertEqual(store.bonus(c.reading, c.surface, "別"), 6000)
        self.assertEqual(store.bonus(c.reading, c.surface, "左"), 4200)
        self.now -= HALF_LIFE_SECONDS  # clock rollback cannot exceed the cap
        store.sync()
        self.assertLessEqual(store.bonus(c.reading, c.surface, "別"), 6000)

    def test_learned_candidates_match_exhaustive_scoring_with_negative_costs(self):
        lex = lexicon([("か", "蚊", 1, 2, 50), ("か", "科", 1, 2, 80),
                       ("が", "画", 2, 1, -300), ("く", "区", 2, 2, 8000),
                       ("ぐ", "具", 1, 1, 200), ("かく", "書く", 2, 2, 90)],
                      [[0, 300, -50], [500, 0, 70], [-100, 600, -40]])
        store = self.store()
        store.confirm([(Candidate("科区", "かく", 0, 1, 2, word_cost=8150), "左")], lex)
        ts = [{"kind": "amb", "faces": {"left": "か", "right": "が"}},
              {"kind": "amb", "faces": {"left": "く", "right": "ぐ"}}]
        for left in range(3):
            for right in range(3):
                model = store.snapshot()
                expected = {}

                def walk(pos, rid, score, reading, surface):
                    if pos == len(ts):
                        cost = score + _end_cost(lex, rid, right) - model.bonus(reading, surface, "左")
                        expected[surface] = min(expected.get(surface, float("inf")), cost)
                        return
                    for w in _spans_from(ts, pos, lex, model):
                        if not w.unknown:
                            walk(w.end, w.rid, score + _word_cost(lex, rid, w),
                                 reading + w.entry.reading, surface + w.entry.surface)

                walk(0, -1 if left == 0 else left, 0, "", "")
                best = min(expected.values())
                expected = {s: c for s, c in expected.items() if c <= best + COST_WINDOW}
                actual = [c for c in candidates(ts, lex, left, right,
                                               learning=model, left_surface="左")
                          if c.kind == "dictionary"]
                self.assertEqual({c.surface: c.score for c in actual}, expected)
                self.assertEqual([c.score for c in actual], sorted(expected.values()))

    def test_commit_context_autocommit_reset_and_cancel(self):
        s = self.session()
        self.type_reading(s, "こうせい")
        self.choose(s, "校正")
        # Converted text is committed when a new literal arrives.
        self.assertEqual(dispatch(s, {"op": "down", "vk": 48})["commit"], "校正")
        self.assertEqual(len(self.rows()), 1)
        dispatch(s, {"op": "down", "vk": 13})  # literal only: no learning row
        self.type_reading(s, "こうせい")
        dispatch(s, {"op": "down", "vk": 13})
        self.assertEqual(self.rows()[-1]["left"], "0")
        dispatch(s, {"op": "reset"})
        self.assertEqual(s._left_surface, "")
        before = self.path.read_bytes()
        self.type_reading(s, "こうせい")
        dispatch(s, {"op": "down", "vk": 27})  # unconverted Esc discards
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_disabled_and_corrupt_files_and_write_failure(self):
        self.path.unlink()
        s = self.session()
        self.type_reading(s, "こうせい")
        dispatch(s, {"op": "down", "vk": 13})
        self.assertFalse(self.path.exists())
        self.path.write_text('broken\n{"version": 999}\n{"incomplete":', encoding="utf-8")
        store = self.store()
        self.assertIsNone(store.snapshot())
        c = Candidate("校正", "こうせい", 0, 1, 1, word_cost=5000)
        store.confirm([(c, "")], self.lex)
        self.assertEqual(self.store().bonus("こうせい", "校正", ""), 6000)
        s._learning = store
        self.type_reading(s, "こうせい")
        expected = s.composition
        with patch("oyayubi.ime.learning.os.open", side_effect=PermissionError):
            self.assertEqual(dispatch(s, {"op": "down", "vk": 13})["commit"], expected)
        self.assertEqual(store.error, "PermissionError")

    def test_ordinary_decode_never_opens_learning_history(self):
        # A learning file exists and favors a different spelling, but ordinary
        # decoder entry points (used by both evaluation scripts) never touch it.
        row = Confirmation("こうせい", "校正", "", self.now, 0, 1851, 1851)
        self.path.write_text(json.dumps(asdict(row), ensure_ascii=False) + "\n", encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=AssertionError("unexpected history read")):
            self.assertEqual(decode_tokens(tokens("こうせい"), self.lex)[0].surface, "構成")
            self.assertEqual(nbest_tokens(tokens("こうせい"), self.lex)[0][0].surface, "構成")


if __name__ == "__main__":
    unittest.main()
