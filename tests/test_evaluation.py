from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oyayubi.lattice import mozc_dict
from oyayubi.lattice.mozc_dict import Entry, MozcLex, apply_user_lex
from oyayubi.lattice.token_viterbi import decode_tokens, nbest_tokens
from tools.wp0.run_ajimee_eval import chunks, convert, hiragana
from tools.wp0.run_mozc_eval import criterion_passes, parse_criterion


class EvaluationTest(unittest.TestCase):
    def test_command_criteria(self):
        for command, argument, expected in (
            ("Conversion Expected", "正解", True),
            ("Conversion Expected 2", "第二", True),
            ("Conversion Expected 2", "第三", False),
            ("Conversion Not Expected 2", "第三", True),
            ("Conversion Not Expected", "正解", False),
            ("Conversion Match", "解", True),
            ("Conversion Not Match", "解", False),
            ("Conversion Not Match", "誤", True),
        ):
            with self.subTest(command=command, argument=argument):
                self.assertEqual(criterion_passes(parse_criterion(command), argument,
                                                  "正解", ["正解", "第二", "第三"]), expected)
        self.assertIsNone(parse_criterion("Suggestion Not Expected"))
        self.assertEqual(parse_criterion("Conversion Expected 999"), ("Expected", False, 999))
        with self.assertRaises(ValueError):
            parse_criterion("Conversion Expected 0")

    def test_uncapped_dictionary_and_user_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dictionary00.txt").write_text(
                "".join(f"こうせい\t0\t0\t{i}\t候補{i}\n" for i in range(20)), encoding="utf-8")
            # A missing file stops the parse (REDESIGN §6.2); this one has no entries.
            (root / "suffix.txt").write_text("", encoding="utf-8")
            with patch.object(mozc_dict, "DICT_DIR", root):
                entries, prefixes = mozc_dict._parse_dict_files()
            lex = MozcLex(entries, prefixes, [0] * 4, 2)
            user = root / "user.jsonl"
            user.write_text(json.dumps(dict(reading="こうせい", surface="追加", cost=100,
                                            lid=0, rid=0)), encoding="utf-8")
            apply_user_lex(lex, user)
            self.assertEqual(len(lex.by_reading["こうせい"]), 21)
            tokens = [{"kind": "plain", "kana": c} for c in "こうせい"]
            best, _ = decode_tokens(tokens, lex)
            self.assertEqual(best.surface, "候補0")
            results, _ = nbest_tokens(tokens, lex, k=100)
            self.assertEqual({r.surface for r in results}, {f"候補{i}" for i in range(20)} | {"追加"})

    def test_ajimee_symbol_boundaries_and_katakana(self):
        reverse = {c: {"kind": "plain", "kana": c} for c in "かな"}
        reading = hiragana("カナ、A1。")
        self.assertEqual(chunks(reading, reverse), [(True, "かな"), (False, "、"),
                                                   (False, "A"), (False, "1"), (False, "。")])
        lex = MozcLex({"かな": [Entry("仮名", "かな", 0, 0, 1)]}, {"か", "かな"}, [0] * 4, 2)
        for mode in ("amb", "una"):
            surface, rd, _, _ = convert(reading, mode, reverse, lex)
            self.assertEqual(surface, "仮名、A1。")
            self.assertEqual(rd, reading)


if __name__ == "__main__":
    unittest.main()
