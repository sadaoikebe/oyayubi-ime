"""Managing the learning history file (D2)."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from yuragi.ime.bunsetsu import Candidate
from yuragi.ime.learning import LearningStore
from yuragi.ime.learning_admin import clear_history, compact, forget, pairs, search
from yuragi.lattice.mozc_dict import Entry, MozcLex

LEX = MozcLex({"かく": [Entry("書く", "かく", 1, 1, 3000)]}, {"か", "かく"}, [0] * 4, 2)


class LearningAdminTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "learning.jsonl"
        self.path.touch()
        self.now = 1000.0
        self.store = LearningStore(self.path, clock=lambda: self.now)
        self.store.confirm([(Candidate("書く", "かく", 0, 1, 1, word_cost=3000), "字を")], LEX)
        self.now = 2000.0
        self.store.confirm([(Candidate("書く", "かく", 0, 1, 1, word_cost=3000), "絵を")], LEX)
        self.now = 3000.0
        # A kana spelling absent from the dictionary: a learned user word.
        self.undo = self.store.confirm([(Candidate("カク", "かく", 0, 1, 1, "kana"), "")], LEX)
        self.now = 4000.0
        self.store.confirm([(Candidate("ぬえ", "ぬえ", 0, 1, 1, "kana"), "")], LEX)

    def tearDown(self):
        self.dir.cleanup()

    def test_list_and_search(self):
        self.assertEqual([(p.reading, p.surface, p.contexts, p.last, p.user_word)
                          for p in pairs(self.path)],
                         [("ぬえ", "ぬえ", 1, 4000.0, True), ("かく", "カク", 1, 3000.0, True),
                          ("かく", "書く", 2, 2000.0, False)])
        self.assertEqual([p.surface for p in search(self.path, "書")], ["書く"])
        self.assertEqual(len(search(self.path, "かく")), 2)

    def test_forget_matches_f6(self):
        self.assertTrue(forget(self.path, "かく", "書く"))
        self.assertFalse(forget(self.path, "かく", "書く"))  # nothing left to forget
        self.assertEqual([p.surface for p in pairs(self.path)], ["ぬえ", "カク"])

    def test_compact_keeps_the_replay(self):
        self.store.undo(self.undo)
        forget(self.path, "ぬえ", "ぬえ")
        before = [(p.reading, p.surface, p.contexts) for p in pairs(self.path)]
        self.assertEqual(compact(self.path), (6, 2))
        self.assertEqual([(p.reading, p.surface, p.contexts) for p in pairs(self.path)], before)
        # The running store sees the new file and keeps appending to it.
        self.store.confirm([(Candidate("書く", "かく", 0, 1, 1, word_cost=3000), "")], LEX)
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 3)

    def test_clear_history(self):
        self.assertEqual(clear_history(self.path), 2)
        self.assertEqual([p.surface for p in pairs(self.path)], ["ぬえ", "カク"])
        self.assertEqual(clear_history(self.path, keep_user_words=False), 0)
        self.assertEqual(pairs(self.path), [])
        self.assertTrue(self.path.exists())  # still enabled, just empty

    def test_missing_file(self):
        self.path.unlink()
        self.assertEqual(pairs(self.path), [])
        with self.assertRaises(FileNotFoundError):
            compact(self.path)
        with self.assertRaises(FileNotFoundError):
            clear_history(self.path)


if __name__ == "__main__":
    unittest.main()
