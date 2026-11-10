from __future__ import annotations

import json
from pathlib import Path
import unittest

from tests.transcript import run_transcript
from tools.wp0.mozc_dict import load_lex

CASES = sorted((Path(__file__).parent / "transcripts").glob("*.json"))
if not CASES:
    raise RuntimeError("no IME transcripts found")


class TranscriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_lex = (load_lex() if any("lexicon" not in json.loads(p.read_text(encoding="utf-8"))
                                        for p in CASES) else None)


def transcript_test(path):
    def test(self):
        run_transcript(self, path, self.real_lex)
    return test


for path in CASES:
    setattr(TranscriptTest, "test_" + path.stem, transcript_test(path))

if __name__ == "__main__":
    unittest.main()
