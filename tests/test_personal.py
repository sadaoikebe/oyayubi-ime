"""The personal word list and its live overlay (D1)."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from oyayubi.ime.personal import (DEFAULT_COST, POS_IDS, PersonalLexicon, add_word,
                                  read_words, remove_word, scan_words)
from oyayubi.lattice.mozc_dict import Entry, MozcLex


def lex():
    return MozcLex({"かた": [Entry("肩", "かた", 1851, 1851, 3000),
                            Entry("方", "かた", 1852, 1852, 3500),
                            Entry("方", "かた", 1900, 1900, 4000)]},
                   {"か", "かた"}, [0] * (3000 * 3000), 3000)


class TempFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "personal.jsonl"

    def tearDown(self):
        self.dir.cleanup()


class PersonalFileTest(TempFile):
    def test_add_replace_remove(self):
        add_word(self.path, "おやゆび", "親指")
        add_word(self.path, "いけべ", "池辺", "姓")
        add_word(self.path, "おやゆび", "親指", "名詞", 1000)  # replaces, not duplicates
        self.assertEqual([(w.reading, w.surface, w.pos, w.cost) for w in read_words(self.path)],
                         [("いけべ", "池辺", "姓", DEFAULT_COST),
                          ("おやゆび", "親指", "名詞", 1000)])
        self.assertTrue(remove_word(self.path, "いけべ", "池辺"))
        self.assertFalse(remove_word(self.path, "いけべ", "池辺"))
        self.assertEqual(len(read_words(self.path)), 1)

    def test_rejects_bad_input(self):
        for args in (("oyayubi", "親指"), ("", "親指"), ("おや", ""), ("おや", " 親"),
                     ("おや", "親", "動詞")):
            with self.assertRaises(ValueError, msg=args):
                add_word(self.path, *args)
        self.assertFalse(self.path.exists())

    def test_hand_edits_tolerated(self):
        self.path.write_text('# comment\n{"reading": "あ", "surface": "亜"}\nnot json\n'
                             '{"reading": "abc", "surface": "x"}\n', encoding="utf-8")
        self.assertEqual([(w.surface, w.cost) for w in read_words(self.path)],
                         [("亜", DEFAULT_COST)])

    def test_skipped_lines_are_counted(self):
        """REDESIGN §6.2: a broken line is skipped and counted (shown to the user)."""
        self.path.write_bytes('{"reading": "あ", "surface": "亜"}\nnot json\n'
                              '{"reading": "い"}\n'.encode("utf-8") + b"\xff\xfe broken\n")
        words, skipped = scan_words(self.path)
        self.assertEqual(([w.surface for w in words], skipped), (["亜"], 3))
        overlay = PersonalLexicon(self.path)
        overlay.sync(lex())
        self.assertEqual((overlay.count, overlay.skipped), (1, 3))


class OverlayTest(TempFile):
    def test_live_overlay_and_restore(self):
        lx = lex()
        original = lx.by_reading["かた"]
        personal = PersonalLexicon(self.path)
        personal.sync(lx)  # no file: nothing changes
        self.assertIs(lx.by_reading["かた"], original)
        add_word(self.path, "かた", "型", "名詞", 2000)
        add_word(self.path, "かた", "方", "名詞", 1000)
        add_word(self.path, "おやゆび", "親指")
        personal.sync(lx)
        self.assertEqual([(e.surface, e.lid, e.cost) for e in lx.by_reading["かた"]],
                         [("方", 1852, 1000), ("型", 1851, 2000), ("肩", 1851, 3000),
                          ("方", 1900, 4000)])
        self.assertEqual(lx.by_reading["おやゆび"][0].lid, POS_IDS["名詞"])
        self.assertIn("おやゆ", lx.prefixes)
        self.assertEqual(original[1].cost, 3500)  # shared originals are not mutated
        self.assertEqual((personal.count, personal.revision), (3, 2))
        revision = personal.revision
        personal.sync(lx)  # unchanged file: no work
        self.assertEqual(personal.revision, revision)
        remove_word(self.path, "かた", "型")
        remove_word(self.path, "かた", "方")
        os.utime(self.path, ns=(1, 1))  # a same-size rewrite still changes the signature
        personal.sync(lx)
        self.assertIs(lx.by_reading["かた"], original)
        self.path.unlink()
        personal.sync(lx)
        self.assertNotIn("おやゆび", lx.by_reading)
        self.assertEqual(personal.count, 0)


if __name__ == "__main__":
    unittest.main()
