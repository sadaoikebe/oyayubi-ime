"""Dictionary loading keeps zero-row penalties out of the on-disk cache."""

import os
import pickle
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yuragi.lattice import mozc_dict


class ZeroRowPenaltyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "lex.pkl"
        # Rows are left RIDs; columns are right LIDs. Only RID=1 is all zero.
        self.raw = mozc_dict.MozcLex({}, set(), [2, 0, 4, 0, 0, 0, 0, 5, 0], 3)
        self.cache.write_bytes(pickle.dumps(self.raw))
        self.cache_patch = patch.object(mozc_dict, "CACHE", self.cache)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.env_patch = patch.dict(os.environ)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        os.environ.pop("ZERO_ROW_PENALTY", None)

    def test_default_penalizes_whole_left_row_including_eos(self):
        before = self.cache.read_bytes()
        lex = mozc_dict.load_lex(user_lexicon=False)
        self.assertEqual(lex.connect, [2, 0, 4, 3000, 3000, 3000, 0, 5, 0])
        self.assertEqual(lex.transition(1, mozc_dict.BOS_ID), 3000)
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertEqual(mozc_dict.load_lex(user_lexicon=False).connect, lex.connect)

    def test_env_zero_disables_penalty_on_next_load(self):
        mozc_dict.load_lex(user_lexicon=False)
        os.environ["ZERO_ROW_PENALTY"] = "0"
        self.assertEqual(mozc_dict.load_lex(user_lexicon=False).connect, self.raw.connect)

    def test_env_can_reproduce_experiment_penalty(self):
        os.environ["ZERO_ROW_PENALTY"] = "1000"
        lex = mozc_dict.load_lex(user_lexicon=False)
        self.assertEqual(lex.connect[3:6], [1000] * 3)

    def test_rebuilt_cache_also_stays_raw(self):
        with (patch.object(mozc_dict, "_parse_dict_files", return_value=({}, set())),
              patch.object(mozc_dict, "_parse_connection",
                           return_value=(self.raw.connect.copy(), self.raw.pos_size))):
            lex = mozc_dict.load_lex(force=True, user_lexicon=False)
        self.assertEqual(lex.connect[3:6], [3000] * 3)
        self.assertEqual(pickle.loads(self.cache.read_bytes()).connect, self.raw.connect)


class MissingFileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in mozc_dict.LOADER_FILES:
            (self.root / name).write_text("", encoding="utf-8")
        self.cache = self.root / "lex.pkl"
        self.addCleanup(patch.stopall)
        patch.object(mozc_dict, "DICT_DIR", self.root).start()
        patch.object(mozc_dict, "CACHE", self.cache).start()

    def test_missing_suffix_stops_the_build_and_names_the_fix(self):
        (self.root / "suffix.txt").unlink()
        (self.root / "dictionary03.txt").unlink()
        with self.assertRaises(FileNotFoundError) as ctx:
            mozc_dict.load_lex(user_lexicon=False)
        message = str(ctx.exception)
        self.assertIn("dictionary03.txt, suffix.txt", message)
        self.assertIn("python -m tools.wp0.fetch_mozc_dict", message)
        self.assertFalse(self.cache.exists())

    def test_pos_names_names_missing_id_def(self):
        from yuragi.ime import bunsetsu
        bunsetsu._pos_names.cache_clear()
        self.addCleanup(bunsetsu._pos_names.cache_clear)
        with patch.object(bunsetsu, "DICT_DIR", self.root):
            with self.assertRaises(FileNotFoundError) as ctx:
                bunsetsu._pos_names()
        self.assertIn("id.def", str(ctx.exception))
        self.assertIn("python -m tools.wp0.fetch_mozc_dict", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
