"""REDESIGN §6.7: the learning history's incremental index, compaction and cost.

The index a store builds by applying its own and other writers' additions must
be exactly the one a fresh full read of the file builds (same rows, same order),
because conversion and prediction results depend on it.
"""
from __future__ import annotations

from dataclasses import asdict
import gc
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from oyayubi.ime import learning
from oyayubi.ime.bunsetsu import Candidate
from oyayubi.ime.learning import (COMPACT_MIN_LINES, LEARN_EXPIRE_DAYS, Confirmation,
                                  LearningStore, Suppression, Tombstone)

ROOT = Path(__file__).resolve().parents[1]
DAY = 24 * 60 * 60


def state(view) -> dict:
    """Everything a conversion can observe through a store or a snapshot."""
    readings = sorted({r for (r, _), _ in view._pairs.items()})
    prefixes = {reading[:i] for reading in readings for i in range(1, len(reading) + 1)}
    return {
        "pairs": dict(view._pairs.items()),
        "entries": {r: view._entries.get(r) for r in readings},
        "learned": list(view.learned()),
        "prefixes": {p for p in prefixes | {"x", "かか"} if view.has_prefix(p)},
        "bonus": {(r, s, left): view.bonus(r, s, left)
                  for (r, s), rows in view._pairs.items() for left in ("", "a", "b")},
    }


def write_lines(path: Path, events) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for e in events:
            stream.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")


class IncrementalEqualsFullReadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "learning.jsonl"
        self.path.touch()
        self.now = 1_800_000_000.0

    def store(self):
        return LearningStore(self.path, clock=lambda: self.now)

    def test_random_histories(self):
        # A small merge threshold exercises the shared-base layers too.
        with patch.object(learning, "_LAYER_MERGE", 5):
            for seed in range(12):
                with self.subTest(seed=seed):
                    self.path.write_text("", encoding="utf-8")
                    self.run_history(random.Random(seed))

    def run_history(self, rnd: random.Random):
        live = self.store()
        ids: list[str] = []
        snapshots = []
        for step in range(220):
            self.now += rnd.choice((1, 60, 3600))
            kind = rnd.random()
            reading = rnd.choice(("か", "かき", "かきく", "さ", "さし"))
            surface = rnd.choice(("A", "B", "C"))
            if kind < 0.70:
                record_id = rnd.choice(("", f"id{step}", f"id{step}"))
                event = Confirmation(reading, surface, rnd.choice(("", "a", "b")), self.now,
                                     rnd.randint(1000, 9000), 1851, 1851,
                                     rnd.random() < 0.2, record_id=record_id)
                if record_id:
                    ids.append(record_id)
            elif kind < 0.80:
                event = Suppression(reading, surface, self.now)
            elif ids:
                event = Tombstone(rnd.sample(ids, min(len(ids), rnd.randint(1, 2))), self.now)
            else:
                continue
            if rnd.random() < 0.5:
                self.assertTrue(live._append([event]))  # this process writes
            else:
                write_lines(self.path, [event])  # another program writes
                if rnd.random() < 0.3:
                    with self.path.open("a", encoding="utf-8") as stream:
                        stream.write("not json\n")
            if rnd.random() < 0.3:
                frozen = live.snapshot()
                if frozen is not None:
                    snapshots.append((frozen, state(frozen)))
            live.sync()
            fresh = self.store()
            self.assertEqual(state(live), state(fresh), f"step {step}")
            self.assertEqual(live.records(), fresh.records())
            self.assertEqual((live.lines, live.skipped), (fresh.lines, fresh.skipped))
        for frozen, seen in snapshots:  # later writes never reached a snapshot
            self.assertEqual(state(frozen), seen)

    def test_replaced_or_shrunk_file_is_read_again(self):
        store = self.store()
        write_lines(self.path, [Confirmation("か", "A", "", self.now, 1, 1, 1, record_id="x")])
        store.sync()
        self.assertTrue(store.has_pair("か", "A"))
        replacement = self.path.with_name("new.jsonl")
        write_lines(replacement, [Confirmation("さ", "B", "", self.now, 1, 1, 1)])
        os.replace(replacement, self.path)
        store.sync()
        self.assertEqual([r.reading for r in store.records()], ["さ"])
        self.path.write_text("", encoding="utf-8")
        store.sync()
        self.assertEqual(store.records(), [])

    def test_unreadable_file_is_read_once_more_after_ten_seconds(self):
        clock = [0.0]
        problems = []
        write_lines(self.path, [Confirmation("か", "A", "", self.now, 1, 1, 1)])
        real_open = open

        def locked(path, *args, **kwargs):
            if Path(path) == self.path:
                raise PermissionError("held by another program")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", locked):
            store = LearningStore(self.path, clock=lambda: self.now,
                                  monotonic=lambda: clock[0], on_problem=problems.append)
            self.assertFalse(store.active)  # no learning now
            clock[0] = 9.9
            store.sync()
            clock[0] = 10.0
            store.sync()  # the one retry, still failing
            self.assertEqual(problems, ["learning-read"])
            clock[0] = 100
            store.sync()
            self.assertEqual(problems, ["learning-read"])  # told once
        clock[0] = 0
        with patch("builtins.open", locked):
            store = LearningStore(self.path, clock=lambda: self.now,
                                  monotonic=lambda: clock[0], on_problem=problems.append)
        clock[0] = 10
        store.sync()  # the retry succeeds
        self.assertTrue(store.active)
        self.assertTrue(store.has_pair("か", "A"))

    def test_failed_write_still_learns_in_memory_and_tells_once(self):
        problems = []
        store = LearningStore(self.path, clock=lambda: self.now, on_problem=problems.append)
        c = Candidate("校正", "こうせい", 0, 1851, 1851, word_cost=5000)
        with patch("oyayubi.ime.learning.os.write", side_effect=OSError(28, "No space")):
            self.assertEqual(store.confirm([(c, "")], None), [])  # nothing to undo
            store.confirm([(c, "x")], None)
        self.assertEqual(problems, ["learning-write"])
        self.assertEqual(store.bonus("こうせい", "校正", ""), 6000)
        store.confirm([(c, "")], None)  # the disk has room again
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)


class CostTest(unittest.TestCase):
    """REDESIGN §6.7: one commit adds at most 5 ms with 100,000 lines of history."""

    def test_commit_cost_with_100k_lines(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "learning.jsonl"
        rnd = random.Random(7)
        kana = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"
        pairs = [("".join(rnd.choice(kana) for _ in range(rnd.randint(2, 6))), f"語{i}")
                 for i in range(30000)]
        lines = []
        for i in range(100_000):
            r, s = rnd.choice(pairs)
            lines.append(json.dumps(asdict(Confirmation(
                r, s, rnd.choice(("", "は", "を")), 1_700_000_000.0 + i, 5000, 1851, 1851,
                record_id=f"{i:032x}")), ensure_ascii=False) + "\n")
        path.write_text("".join(lines), encoding="utf-8")
        store = LearningStore(path)
        self.assertEqual(store.lines, 100_000)
        # As the engine does after loading (engine.py): long-lived objects
        # stay out of the collector's full passes.
        gc.freeze()
        self.addCleanup(gc.unfreeze)
        times = []
        # New words every time, past a layer merge (_LAYER_MERGE changed keys).
        for i in range(4500):
            reading = "こうせい" + kana[i % 45] + kana[i // 45 % 45]
            c = Candidate(f"校正{i}", reading, 0, 1851, 1851, word_cost=5000)
            store.snapshot()  # a keystroke before the commit
            start = time.perf_counter()
            self.assertTrue(store.confirm([(c, "")], None))
            store.snapshot()  # the next keystroke after it
            times.append((time.perf_counter() - start) * 1000)
        times.sort()
        print(f"\ncommit with 100k lines: median {times[2250]:.3f} ms, "
              f"p95 {times[4275]:.3f} ms, max {times[-1]:.3f} ms", file=sys.stderr)
        self.assertLessEqual(times[-1], 5.0)
        self.assertEqual(store.lines, 104_500)


class CompactionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "learning.jsonl"
        self.now = 1_800_000_000.0

    def history(self):
        """6,000 lines over 50 live records; one old taught word, one old record."""
        events = []
        for i in range(6000):
            events.append(Confirmation(f"よみ{i % 50}", "表記", "", self.now - 6000 + i,
                                       5000, 1851, 1851, record_id=f"r{i}"))
        old = self.now - (LEARN_EXPIRE_DAYS + 1) * DAY
        events.append(Confirmation("ふるい", "古い", "", old, 5000, 1851, 1851))
        events.append(Confirmation("ぬえ", "ヌエ", "", old, 6000, 1851, 1851, True))
        write_lines(self.path, events)
        return events

    def test_compacts_to_the_live_records_and_keeps_a_backup(self):
        self.history()
        original = self.path.read_bytes()
        store = LearningStore(self.path, clock=lambda: self.now)
        before = state(store)
        self.assertEqual(store.compact_if_needed(), (6002, 51))
        self.assertEqual(self.path.with_name("learning.jsonl.bak").read_bytes(), original)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(store.records()))  # lines == live records
        self.assertEqual(store.lines, 51)
        self.assertFalse(store.has_pair("ふるい", "古い"))  # expired
        self.assertTrue(store.has_pair("ぬえ", "ヌエ"))  # taught words never expire
        after = state(store)
        before["pairs"].pop(("ふるい", "古い"))
        self.assertEqual(after["pairs"], before["pairs"])
        self.assertIsNone(store.compact_if_needed())  # nothing more to do

    def test_small_or_dense_files_are_left_alone(self):
        write_lines(self.path, [Confirmation(f"よみ{i}", "表記", "", self.now, 1, 1, 1)
                                for i in range(COMPACT_MIN_LINES + 10)])
        self.assertIsNone(LearningStore(self.path).compact_if_needed())  # all live
        self.path.write_text("", encoding="utf-8")
        write_lines(self.path, [Confirmation("よみ", "表記", "", self.now, 1, 1, 1)] * 100)
        self.assertIsNone(LearningStore(self.path).compact_if_needed())  # small

    def test_killed_at_any_point_leaves_a_whole_file(self):
        """The process dies (no clean-up) at each step of the rewrite."""
        self.history()
        original = self.path.read_bytes()
        expected = None
        for point in ("write", "fsync", "link", "replace", "end"):
            with self.subTest(point=point):
                self.path.write_bytes(original)
                for leftover in (".tmp", ".bak"):
                    self.path.with_name(self.path.name + leftover).unlink(missing_ok=True)
                code = subprocess.run(
                    [sys.executable, "-c", KILL_SCRIPT, str(self.path), point, str(self.now)],
                    cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(code.returncode, 0 if point == "end" else 9, code.stderr)
                result = self.path.read_bytes()
                if point == "end":
                    expected = result
                    self.assertNotEqual(result, original)
                self.assertIn(result, (original, expected) if expected else (original,
                                                                              result))
                # Either way the file reads back whole: every line is valid.
                store = LearningStore(self.path, clock=lambda: self.now)
                self.assertEqual(store.skipped, 0)
                self.assertTrue(result == original or store.lines == len(store.records()))
        # The interrupted runs left the original; a run to the end the compacted file.
        self.assertIsNotNone(expected)


KILL_SCRIPT = r"""
import os, sys
from unittest.mock import patch
from oyayubi.ime.learning import LearningStore
path, point, now = sys.argv[1], sys.argv[2], float(sys.argv[3])
def die(*a, **k):
    os._exit(9)  # like a kill: no finally blocks, no flushing
real = {"fsync": os.fsync, "link": os.link, "replace": os.replace}
def at(name):
    def step(*a, **k):
        if name == point:
            die()
        return real[name](*a, **k)
    return step
store = LearningStore(path, clock=lambda: now)
if point == "write":
    import builtins
    real_open = builtins.open
    def opener(file, mode="r", *a, **k):
        if str(file).endswith(".tmp"):
            stream = real_open(file, mode, *a, **k)
            stream.write(b'{"partial":')
            stream.flush()
            die()
        return real_open(file, mode, *a, **k)
    patch("builtins.open", opener).start()
with patch("os.fsync", at("fsync")), patch("os.link", at("link")), \
        patch("os.replace", at("replace")):
    store.compact_if_needed()
"""


if __name__ == "__main__":
    unittest.main()
