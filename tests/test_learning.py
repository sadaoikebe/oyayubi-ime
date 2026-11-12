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
from oyayubi.lattice.mozc_dict import Entry, MozcLex, load_lex
from oyayubi.lattice.table import reverse_table
from oyayubi.lattice.token_viterbi import decode_tokens, end_cost, nbest_tokens, spans_from, word_cost
from tests.test_bunsetsu import lexicon


def tokens(reading):
    return [{"kind": "plain", "kana": c} for c in reading]


def press(s, vk):
    """Unmodified key down and up; returns the down reply (F6/F7 act on down)."""
    reply = dispatch(s, {"op": "down", "vk": vk})
    dispatch(s, {"op": "up", "vk": vk})
    return reply


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

    def test_number_and_date_rows_are_never_learned(self):
        store = self.store()
        rows = [(Candidate("一二", "12", 0, 0, 0, "number"), ""),
                (Candidate("2026/11/12", "きょう", 0, 1851, 1851, "date", 3000), "")]
        self.assertEqual(store.confirm(rows, self.lex), [])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")
        s = self.session()
        self.type_reading(s, "きょう")
        s.converted = True
        s._open_cands()
        stream = s.segments[s.focus].choices
        stream.last()
        date = next(c.surface for c in stream.cache if c.kind == "date")
        self.choose(s, date, kind="date")
        self.assertEqual(s._commit(), date)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")

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
                        cost = score + end_cost(lex, rid, right) - model.bonus(reading, surface, "左")
                        expected[surface] = min(expected.get(surface, float("inf")), cost)
                        return
                    for w in spans_from(ts, pos, lex, model):
                        if not w.unknown:
                            walk(w.end, w.rid, score + word_cost(lex, rid, w),
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

    def test_ordinary_decode_never_opens_learning_history(self):
        # A learning file exists and favors a different spelling, but ordinary
        # decoder entry points (used by both evaluation scripts) never touch it.
        row = Confirmation("こうせい", "校正", "", self.now, 0, 1851, 1851)
        self.path.write_text(json.dumps(asdict(row), ensure_ascii=False) + "\n", encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=AssertionError("unexpected history read")):
            self.assertEqual(decode_tokens(tokens("こうせい"), self.lex)[0].surface, "構成")
            self.assertEqual(nbest_tokens(tokens("こうせい"), self.lex)[0][0].surface, "構成")

    def test_forget_restores_ranking_and_confirmation_relearns(self):
        s = self.session()
        self.type_reading(s, "こうせい")
        self.choose(s, "構成")
        baseline = list(s.candidates)
        self.choose(s, "校正")
        dispatch(s, {"op": "down", "vk": 13})
        learned = self.path.read_bytes()

        s = self.session()
        self.type_reading(s, "こうせい")
        self.choose(s, "校正")
        self.assertEqual(s.candidates[0], "校正")
        reply = dispatch(s, {"op": "down", "vk": 0x75})  # F6
        self.assertEqual((reply["composition"], reply["commit"]), ("構成", ""))
        self.assertEqual(reply["candidates"], baseline)
        dispatch(s, {"op": "down", "vk": 0x75})  # repeat must not suppress 構成 too
        dispatch(s, {"op": "up", "vk": 0x75})
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.rows()[-1]["type"], "suppress")
        self.assertTrue(self.path.read_bytes().startswith(learned))
        self.assertEqual(self.store().bonus("こうせい", "校正", ""), 0)
        other = self.session()
        self.type_reading(other, "こうせい")
        self.choose(other, "構成")
        self.assertEqual(other.candidates, baseline)

        self.choose(s, "校正")
        self.assertEqual(dispatch(s, {"op": "down", "vk": 13})["commit"], "校正")
        other = self.session()
        self.type_reading(other, "こうせい")
        self.assertEqual(other.composition, "校正")
        self.choose(other, "校正")
        self.assertEqual(other.candidates[0], "校正")

    def test_suppression_covers_all_contexts_and_legacy_rows_in_file_order(self):
        c = Candidate("校正", "こうせい", 0, 1851, 1851, word_cost=5000)
        # The old on-disk format remains readable; clock rollback never beats
        # later lines, whether positive or suppressing.
        legacy = asdict(Confirmation(c.reading, c.surface, "左", self.now, 7000, 1, 1))
        legacy.pop("record_id")
        self.path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        store, observer = self.store(), self.store()
        self.now -= 100
        store.confirm([(c, "右")], self.lex)
        self.assertEqual([(e.lid, e.cost) for _, e in store.spans(tokens(c.reading), 0, self.lex)],
                         [(1851, 5000)])
        frozen = store.snapshot()
        store.suppress(c.reading, c.surface)
        observer.sync()
        for left in ("左", "右", ""):
            self.assertEqual(observer.bonus(c.reading, c.surface, left), 0)
        self.assertIsNone(observer.snapshot())
        self.assertEqual(frozen.bonus(c.reading, c.surface, "右"), 6000)
        self.now -= 100
        store.confirm([(c, "新")], self.lex)
        self.assertEqual(self.store().bonus(c.reading, c.surface, "新"), 6000)
        self.assertEqual(self.store().bonus(c.reading, c.surface, "左"), 2400)
        # Undoing the new confirmation cannot cross the suppression barrier.
        store.undo([self.rows()[-1]["record_id"]])
        self.assertIsNone(self.store().snapshot())

    def test_undo_last_confirmation_removes_all_bunsetsu_bonuses(self):
        s = self.session()
        self.type_reading(s, "きかんのこうせいをした")
        self.choose(s, "校正を", reading="こうせいを")
        dispatch(s, {"op": "down", "vk": 13})
        confirmations = self.rows()
        self.assertEqual(len(confirmations), 3)
        self.assertEqual(len({r["record_id"] for r in confirmations}), 3)
        before = self.path.read_bytes()
        reply = press(s, 0x76)
        self.assertEqual((reply["composition"], reply["commit"]), ("", ""))
        self.assertTrue(self.path.read_bytes().startswith(before))
        tombstone = self.rows()[-1]
        self.assertEqual(tombstone["type"], "tombstone")
        self.assertEqual(tombstone["record_ids"], [r["record_id"] for r in confirmations])
        restored = self.store()
        for r in confirmations:
            self.assertEqual(restored.bonus(r["reading"], r["surface"], r["left"]), 0)
        self.assertIsNone(restored.snapshot())
        other = self.session()
        self.type_reading(other, "きかんのこうせいをした")
        self.assertEqual(other.composition, "機関の構成をした")

    def test_repeated_undo_restores_previous_record_then_removes_it(self):
        s = self.session()
        for _ in range(2):
            dispatch(s, {"op": "reset"})  # reset does not discard session undo history
            self.type_reading(s, "こうせい")
            self.choose(s, "校正")
            dispatch(s, {"op": "down", "vk": 13})
            self.now += HALF_LIFE_SECONDS
        self.assertEqual(self.store().bonus("こうせい", "校正", ""), 3000)
        dispatch(s, {"op": "down", "vk": 0x76})  # F7 after commit, no candidates
        self.assertEqual(self.store().bonus("こうせい", "校正", ""), 1500)
        dispatch(s, {"op": "down", "vk": 0x76})  # repeat must not undo the next group
        dispatch(s, {"op": "up", "vk": 0x76})
        self.assertEqual(len(self.rows()), 3)
        press(s, 0x76)
        self.assertEqual(self.store().bonus("こうせい", "校正", ""), 0)
        before = self.path.read_bytes()
        press(s, 0x76)
        self.assertEqual(self.path.read_bytes(), before)

    def test_undo_targets_own_records_without_removing_other_session_learning(self):
        s, other = self.session(), self.session()
        for session in (s, other):
            self.type_reading(session, "こうせい")
            self.choose(session, "校正")
            dispatch(session, {"op": "down", "vk": 13})
        press(s, 0x76)
        self.assertEqual(self.store().bonus("こうせい", "校正", ""), 6000)
        press(other, 0x76)
        self.assertEqual(self.store().bonus("こうせい", "校正", ""), 0)

    def test_autocommit_is_undoable_but_literals_and_cancellations_add_no_group(self):
        s = self.session()
        self.type_reading(s, "こうせい")
        self.choose(s, "校正")
        self.assertEqual(dispatch(s, {"op": "down", "vk": 48})["commit"], "校正")
        dispatch(s, {"op": "down", "vk": 13})  # literal-only confirmation
        self.type_reading(s, "こうせい")
        dispatch(s, {"op": "down", "vk": 27})
        press(s, 0x76)
        self.assertEqual(self.rows()[-1]["record_ids"], [self.rows()[0]["record_id"]])
        before = self.path.read_bytes()
        press(s, 0x76)
        self.assertEqual(self.path.read_bytes(), before)

    def test_undo_and_forget_preserve_other_selections_and_resized_intervals(self):
        s = self.session()
        self.type_reading(s, "きかんのこうせいをした")
        self.choose(s, "校正を", reading="こうせいを")
        dispatch(s, {"op": "down", "vk": 13})
        dispatch(s, {"op": "reset"})
        self.type_reading(s, "きかんのこうせいをした")
        self.choose(s, "校正を", reading="こうせいを")
        dispatch(s, {"op": "down", "vk": 39, "mods": ["shift"]})
        dispatch(s, {"op": "up", "vk": 39})
        before, focus = s.segment_snapshot(), s.focus
        press(s, 0x76)
        self.assertEqual((s.segment_snapshot(), s.focus), (before, focus))
        press(s, 0x75)
        self.assertEqual([(r["start"], r["end"]) for r in s.segment_snapshot()],
                         [(r["start"], r["end"]) for r in before])
        for i, row in enumerate(s.segment_snapshot()):
            if i != focus:
                self.assertEqual(row["surface"], before[i]["surface"])
                self.assertEqual(row["fixed"], before[i]["fixed"])

    def test_learning_operations_disabled_and_failed_writes_can_retry(self):
        s = self.session()
        self.type_reading(s, "こうせい")
        self.choose(s, "校正")
        dispatch(s, {"op": "down", "vk": 13})
        dispatch(s, {"op": "reset"})
        self.type_reading(s, "こうせい")
        self.choose(s, "校正")
        before, composition = self.path.read_bytes(), s.composition
        with patch("oyayubi.ime.learning.os.open", side_effect=PermissionError):
            self.assertFalse(s.forget_candidate())
            self.assertFalse(s.undo_learning())
        self.assertEqual((self.path.read_bytes(), s.composition), (before, composition))
        self.assertTrue(s.undo_learning())  # failed write did not pop the undo group
        self.path.unlink()
        self.assertFalse(s.forget_candidate())
        self.assertFalse(self.path.exists())
        disabled = ImeSession(learning_path=None)
        for vk in (0x75, 0x76):
            self.assertEqual(press(disabled, vk)["commit"], "")

    def test_learning_keys_require_unmodified_input_and_forget_requires_candidates(self):
        s = self.session()
        self.type_reading(s, "こうせい")
        self.choose(s, "校正")
        dispatch(s, {"op": "down", "vk": 13})
        dispatch(s, {"op": "reset"})
        self.type_reading(s, "こうせい")
        before = self.path.read_bytes()
        dispatch(s, {"op": "down", "vk": 0x75})  # no candidate window
        self.choose(s, "校正")
        for mods in (["shift"], ["ctrl"], ["alt"], ["win"]):
            for vk in (0x75, 0x76):
                dispatch(s, {"op": "down", "vk": vk, "mods": mods})
                dispatch(s, {"op": "up", "vk": vk, "mods": mods})
        self.assertEqual(self.path.read_bytes(), before)
        dispatch(s, {"op": "down", "vk": 16})  # physical Shift tracking too
        dispatch(s, {"op": "down", "vk": 0x76})
        dispatch(s, {"op": "up", "vk": 0x76})
        dispatch(s, {"op": "up", "vk": 16})
        dispatch(s, {"op": "set_direct", "direct": True})
        for vk in (0x75, 0x76):
            self.assertTrue(dispatch(s, {"op": "down", "vk": vk})["pass_through"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_control_records_do_not_hide_valid_confirmations(self):
        store = self.store()
        c = Candidate("校正", "こうせい", 0, 1851, 1851, word_cost=5000)
        ids = store.confirm([(c, "")], self.lex)
        invalid = [
            {"type": "suppress", "reading": c.reading, "surface": c.surface,
             "timestamp": self.now, "version": 999},
            {"type": "tombstone", "record_ids": ids, "timestamp": "bad"},
            {"type": "tombstone", "record_ids": [42], "timestamp": self.now},
            {"type": "future", "timestamp": self.now},
        ]
        with self.path.open("a", encoding="utf-8") as stream:
            for row in invalid:
                stream.write(json.dumps(row) + "\n")
            stream.write('{"type":"suppress",')
        self.assertEqual(self.store().bonus(c.reading, c.surface, ""), 6000)
        store.suppress(c.reading, c.surface)  # repairs the missing newline on append
        self.assertEqual(self.store().bonus(c.reading, c.surface, ""), 0)


if __name__ == "__main__":
    unittest.main()
