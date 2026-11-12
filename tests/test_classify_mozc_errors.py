from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from oyayubi.lattice.mozc_dict import Entry, MAX_WORD_MORA, MozcLex, UNK_COST
from oyayubi.lattice.token_viterbi import decode_tokens, nbest_tokens
from tools.wp0 import classify_mozc_errors as diagnostic
from tools.wp0.run_mozc_eval import criterion_passes, parse_criterion


NAMES = {0: "BOS/EOS,*,*", 1: "名詞,一般,*", 2: "助詞,格助詞,*"}


def lexicon(entries, matrix=None):
    matrix = matrix or [[0] * 3 for _ in range(3)]
    by_reading, prefixes = {}, set()
    for reading, surface, cost, *pos in entries:
        lid, rid = pos or (1, 1)
        by_reading.setdefault(reading, []).append(Entry(surface, reading, lid, rid, cost))
        prefixes.update(reading[:i] for i in range(1, len(reading) + 1))
    return MozcLex(by_reading, prefixes, [cost for row in matrix for cost in row], len(matrix))


def source(reading="か", surface="科", command="Conversion Expected", argument=None):
    return {"row_id": "digest:2", "line_number": 2, "input_reading": reading,
            "recorded_surface": surface, "recorded_status": "OK:", "command": command,
            "argument": surface if argument is None else argument,
            "criterion": "not_applicable" if parse_criterion(command) is None else "conversion"}


def condition(row, lex, mode="una", reverse=None):
    return diagnostic.diagnose_condition(row, mode, reverse or {}, lex, set(), NAMES, lambda *_: None)


def classified(row, amb, una=None, state="complete"):
    return {**row, "conditions": {"amb": amb, "una": una or amb}, "state": state}


def slow_condition(row, mode, reverse, lex, overlay, names, emit):
    if row["input_reading"] == "か":
        emit(mode, {"status": "partial", "surface": "蚊", "reading": "か",
                    "reading_match": True, "surface_match": False,
                    "command_pass": None, "command_status": "pending"})
        time.sleep(2)
    return diagnostic_condition(row, mode, reverse, lex, overlay, names, emit)


diagnostic_condition = diagnostic.diagnose_condition


class LedgerTest(unittest.TestCase):
    def test_physical_ids_duplicates_and_original_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evaluation.tsv"
            path.write_text("# comment\n\nOK:\tか\t蚊\tConversion Match\t蚊\t1\n"
                            "FAILED:\tか\t科\tConversion Not Match\t蚊\t2\n", encoding="utf-8")
            rows = diagnostic.read_rows(path)
            self.assertEqual([r["line_number"] for r in rows], [3, 4])
            self.assertNotEqual(rows[0]["row_id"], rows[1]["row_id"])
            self.assertEqual(rows[0]["row_id"], f"{diagnostic.sha256(path)}:3")
            self.assertNotEqual(rows[0]["content_sha256"], rows[1]["content_sha256"])
            self.assertEqual(rows[1]["recorded_status"], "FAILED:")
            self.assertEqual(rows[1]["recorded_version"], "2")
            self.assertEqual(rows[1]["criterion"], {"kind": "Match", "negative": True, "topn": 1})

    def test_excluded_characters_and_nonconversion_stay_in_ledger(self):
        lex = lexicon([("か", "科", 1)])
        reverse = {"か": diagnostic.plain_tokens("か")[0]}
        rows = [source("か!"), source(command="Suggestion Not Expected")]
        results = list(diagnostic.run_rows(rows, lex, set(), NAMES, reverse, timeout=1))
        self.assertEqual(results[0]["unencodable"], [{"position": 1, "character": "!"}])
        self.assertEqual(results[0]["automatic_classification"]["primary"], "input_excluded")
        self.assertEqual(results[1]["conditions"]["amb"]["command_status"], "not_applicable")
        self.assertIsNone(results[1]["conditions"]["amb"]["command_pass"])
        self.assertTrue(results[1]["conditions"]["amb"]["surface_match"])
        self.assertEqual(results[1]["automatic_classification"]["primary"], "criterion_review")


class ReachabilityTest(unittest.TestCase):
    def test_composed_path_is_not_a_missing_word_and_costs_are_accounted(self):
        matrix = [[0, 5, 2], [7, 11, 13], [17, 19, 23]]
        lex = lexicon([("か", "科", 10), ("く", "区", 20, 2, 2)], matrix)
        row = source("かく", "科区")
        result = condition(row, lex)
        target = result["diagnostics"]["recorded"]
        self.assertFalse(target["direct_whole_string_entry"])
        self.assertTrue(target["dictionary_path_nbest_policy"])
        self.assertEqual(target["rank_exact"], 1)
        self.assertEqual(target["target_path"]["word_cost"], 30)
        self.assertEqual(target["target_path"]["connection_cost"], 5 + 13 + 17)
        self.assertEqual(target["target_path"]["score"], 65)
        self.assertEqual(target["target_path"]["bos_cost"], 5)
        self.assertEqual(target["target_path"]["eos_cost"], 17)
        self.assertNotIn("vocabulary_missing_suspected", diagnostic.classify(classified(row, result))["labels"])

    def test_above_30_is_unresolved_not_absence(self):
        lex = lexicon([("か", f"候補{i}", i) for i in range(35)])
        row = source(surface="候補34")
        result = condition(row, lex)
        target = result["diagnostics"]["recorded"]
        self.assertIsNone(target["rank_exact"])
        self.assertEqual(target["rank_status"], "rank_gt_30_or_unreachable")
        self.assertTrue(target["dictionary_path_nbest_policy"])
        self.assertTrue(target["direct_whole_string_entry"])
        self.assertEqual(diagnostic.classify(classified(row, result))["primary"], "homophone_rank")

    def test_high_cost_and_length_are_generation_constraints(self):
        for reading, cost, reason in (("か", UNK_COST, "nbest_high_cost_exclusion"),
                                     ("か" * (MAX_WORD_MORA + 1), 1, "word_length_limit")):
            with self.subTest(reason=reason):
                lex = lexicon([(reading, "目的", cost), ("か", "蚊", 1)])
                row = source(reading, "目的")
                result = condition(row, lex)
                target = result["diagnostics"]["recorded"]
                self.assertTrue(target["direct_whole_string_entry"])
                self.assertFalse(target["dictionary_path_nbest_policy"])
                self.assertIn(reason, target["generation_reasons"])
                self.assertEqual(diagnostic.classify(classified(row, result))["primary"], "generation_constraint")

    def test_unknown_backoff_is_separate_from_dictionary_path(self):
        lex = lexicon([("か", "蚊", 1)])
        result = condition(source("く", "く"), lex)
        target = result["diagnostics"]["recorded"]
        self.assertFalse(target["dictionary_path_engine_policy"])
        self.assertTrue(target["path_with_engine_unknown_backoff"])
        self.assertEqual(target["target_path"]["pieces"][0]["source_layer"], "unknown_backoff")
        missing = condition(source("か", "架"), lex)
        labels = diagnostic.classify(classified(source("か", "架"), missing))
        self.assertEqual(labels["primary"], "needs_review")
        self.assertNotIn("vocabulary_missing_suspected", labels["labels"])

    def test_dp_keeps_output_offsets_rids_and_negative_connections(self):
        matrix = [[0, 0, 0], [0, 50, -100], [0, 0, 0]]
        lex = lexicon([("か", "甲", 1), ("か", "甲乙", 0, 2, 2),
                       ("く", "乙丙", 1, 2, 2), ("く", "丙", 1)], matrix)
        path = diagnostic.constrained_path("かく", "甲乙丙", lex)
        self.assertEqual(path.score, -98)
        self.assertEqual([p.surface for p in path.pieces], ["甲", "乙丙"])

    def test_pruning_suspicion_requires_a_better_comparable_path(self):
        size = 22
        matrix = [[0] * size for _ in range(size)]
        matrix[20][21] = -5000
        lex = lexicon([("か", f"別POS{i}", i, i, i) for i in range(1, 21)]
                      + [("く", "区", 0, 21, 21)], matrix)
        row = source("かく", "別POS20区")
        names = {i: "名詞,一般,*" for i in range(size)}
        result = diagnostic.diagnose_condition(row, "una", {}, lex, set(), names, lambda *_: None)
        self.assertNotEqual(result["surface"], row["recorded_surface"])
        self.assertTrue(result["diagnostics"]["recorded"]["search_pruning_suspected"])
        self.assertEqual(diagnostic.classify(classified(row, result))["primary"], "search_pruning_suspected")

    def test_word_boundaries_and_ui_boundaries_are_separate(self):
        lex = lexicon([("かく", "書く", 0), ("か", "科", 1), ("く", "区", 1, 2, 2)])
        row = source("かく", "科区")
        result = condition(row, lex)
        diag = result["diagnostics"]["recorded"]
        self.assertTrue(diag["word_boundaries_differ"])
        self.assertFalse(diag["ui_boundaries_differ"])
        self.assertEqual(diag["ui_candidates"][0]["rank"], 2)
        self.assertEqual(diag["ui_candidates"][0]["page"], 1)
        self.assertEqual(diagnostic.classify(classified(row, result))["primary"], "segmentation_suspected")


class CriteriaAndClassificationTest(unittest.TestCase):
    def test_expected_99_reuses_the_existing_evaluation_definition(self):
        lex = lexicon([("か", f"候補{i}", i) for i in range(40)])
        row = source(surface="候補39", command="Conversion Expected 99")
        result = condition(row, lex)
        tokens = diagnostic.plain_tokens("か")
        best, _ = decode_tokens(tokens, lex)
        exact, _ = nbest_tokens(tokens, lex, k=99)
        existing = list(dict.fromkeys([best.surface] + [d.surface for d in exact]))[:99]
        self.assertEqual(result["command_candidates"], existing)
        self.assertEqual(result["command_pass"], criterion_passes(parse_criterion(row["command"]),
                                                                  row["argument"], best.surface, existing))
        self.assertTrue(result["command_pass"])
        self.assertIsNone(result["diagnostics"]["recorded"]["rank_exact"])

    def test_not_argument_is_never_a_target_or_addition(self):
        row = source(surface="科", command="Conversion Not Match", argument="蚊")
        result = condition(row, lexicon([("か", "蚊", 0), ("か", "科", 1)]))
        self.assertEqual(set(result["diagnostics"]), {"recorded"})
        self.assertFalse(result["command_pass"])
        full = classified(row, result)
        full["automatic_classification"] = diagnostic.classify(full)
        self.assertEqual(full["automatic_classification"]["primary"], "criterion_review")
        self.assertIn("Not引数を追加語にしない", diagnostic.work_items([full])[0]["suggested_action"])

    def test_match_pass_is_independent_of_recorded_surface_match(self):
        row = source("かく", "科句", command="Conversion Match", argument="科")
        result = condition(row, lexicon([("かく", "科区", 0)]))
        self.assertTrue(result["command_pass"])
        self.assertFalse(result["surface_match"])
        self.assertEqual(diagnostic.classify(classified(row, result))["primary"], "criterion_review")

    def test_reading_mismatch_has_una_contrast_and_priority(self):
        lex = lexicon([("か", "蚊", 0), ("く", "区", 10)])
        row = source("く", "区")
        reverse = {"く": {"kind": "amb", "faces": {"left": "か", "right": "く"}}}
        amb, una = condition(row, lex, "amb", reverse), condition(row, lex)
        classification = diagnostic.classify(classified(row, amb, una))
        self.assertEqual(classification["primary"], "reading_mismatch")
        self.assertTrue(classification["amb_to_una"]["recorded_surface_recovered"])
        self.assertTrue(classification["amb_to_una"]["una_target_reachable"])
        partial = classified(row, amb, una)
        partial["argument"] = "別の表記"
        self.assertEqual(diagnostic.classify(partial)["primary"], "criterion_review")

    def test_positive_expected_and_recorded_are_distinct_references(self):
        row = source(surface="蚊", command="Conversion Expected 2", argument="科")
        result = condition(row, lexicon([("か", "蚊", 0), ("か", "科", 1)]))
        self.assertTrue(result["surface_match"])
        self.assertTrue(result["command_pass"])
        self.assertEqual(result["diagnostics"]["positive_expected"]["target"], "科")
        self.assertEqual(diagnostic.classify(classified(row, result))["primary"], "criterion_review")

    def test_only_explicit_equivalence_can_classify_a_variant(self):
        for actual, target, expected in (("Ａ", "A", "orthographic_variant"),
                                         ("か\u3099", "が", "orthographic_variant"),
                                         ("①", "1", "homophone_rank")):
            row = source(surface=target)
            result = condition(row, lexicon([("か", actual, 0), ("か", target, 1)]))
            classification = diagnostic.classify(classified(row, result))
            self.assertEqual(classification["primary"], expected)
            self.assertFalse(result["surface_match"])
        row = source(surface="記録", argument="実際")
        result = condition(row, lexicon([("か", "実際", 0)]))
        self.assertNotIn("orthographic_variant", diagnostic.classify(classified(row, result))["labels"])


class ExecutionAndReportTest(unittest.TestCase):
    def test_worklist_groups_the_same_inferred_span_across_sentences(self):
        lex = lexicon([("か", "蚊", 0), ("か", "科", 1), ("く", "区", 0)])
        rows = []
        for i, (reading, surface) in enumerate((("か", "科"), ("かく", "科区"))):
            row = source(reading, surface)
            result = condition(row, lex)
            full = classified(row, result)
            full["row_id"] = f"digest:{i + 2}"
            full["automatic_classification"] = diagnostic.classify(full)
            rows.append(full)
        items = diagnostic.work_items(rows)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["affected_rows"], 2)
        self.assertEqual(items[0]["row_ids"], ["digest:2", "digest:3"])
        self.assertIn("inferred_span:か|蚊=>科", items[0]["pattern"])

    @unittest.skipUnless("fork" in diagnostic.mp.get_all_start_methods(),
                         "the worker must inherit the patched diagnose_condition (fork only)")
    def test_deadline_preserves_partial_evidence_and_restarts_worker(self):
        lex = lexicon([("か", "蚊", 0), ("く", "区", 0)])
        reverse = {ch: diagnostic.plain_tokens(ch)[0] for ch in "かく"}
        with patch.object(diagnostic, "diagnose_condition", slow_condition):
            start = time.monotonic()
            rows = list(diagnostic.run_rows([source(), source("く", "区")], lex, set(), NAMES,
                                            reverse, timeout=0.15))
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(rows[0]["state"], "unknown_timeout")
        self.assertEqual(rows[0]["conditions"]["amb"]["surface"], "蚊")
        self.assertEqual(rows[0]["conditions"]["amb"]["command_status"], "unknown_timeout")
        self.assertIn("unknown_timeout", rows[0]["automatic_classification"]["labels"])
        self.assertEqual(rows[1]["state"], "complete")

    def test_output_totals_unknown_denominators_deltas_and_worklist(self):
        lex = lexicon([("か", "蚊", 0), ("か", "科", 1)])
        base = source()
        amb = condition(base, lex)
        una = {**amb, "surface_match": True, "command_pass": True}
        row = classified(base, amb, una)
        row["automatic_classification"] = diagnostic.classify(row)
        unknown = {**source("く", "区"), "row_id": "digest:3", "state": "unknown_timeout", "conditions": {}}
        unknown["automatic_classification"] = diagnostic.classify(unknown)
        excluded = {**source("!"), "row_id": "digest:4", "state": "input_excluded", "conditions": {}}
        excluded["automatic_classification"] = diagnostic.classify(excluded)
        rows = [row, unknown, excluded]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest = diagnostic.write_reports(output, rows, {"settings": {"user_lexicon": True},
                                                                "elapsed_seconds": 1.0})
            self.assertEqual(sum(manifest["primary_counts"].values()), 3)
            self.assertEqual(manifest["encodable_rows"], 2)
            self.assertEqual(manifest["condition_differences"]["surface_match"]["improved"], ["digest:2"])
            self.assertEqual(manifest["condition_differences"]["surface_match"]["unknown"], ["digest:3"])
            self.assertEqual(len((output / "rows.jsonl").read_text().splitlines()), 3)
            self.assertIn("|合計|3|", (output / "summary.md").read_text())
            self.assertEqual(json.loads((output / "run.json").read_text())["ledger_rows"], 3)
            with (output / "worklist.tsv").open() as stream:
                items = list(csv.DictReader(stream, delimiter="\t"))
            self.assertEqual(sum(int(item["affected_rows"]) for item in items), 2)
            self.assertTrue(all(item["daily_frequency"] == "未確認" for item in items))

    def test_overlay_provenance_and_text_loading_do_not_write_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dictionary00.txt").write_text("か\t1\t1\t10\t科\n", encoding="utf-8")
            (root / "suffix.txt").write_text("", encoding="utf-8")
            (root / "connection_single_column.txt").write_text("2\n0\n0\n0\n0\n", encoding="utf-8")
            (root / "id.def").write_text("0 BOS/EOS,*,*\n1 名詞,一般,*\n", encoding="utf-8")
            user = root / "user.jsonl"
            user.write_text(json.dumps({"reading": "か", "surface": "科", "cost": 1}) + "\n", encoding="utf-8")
            original_dir = diagnostic.mozc_dict.DICT_DIR
            lex, overlay, names = diagnostic.load_snapshot(root, None, user)
            self.assertEqual(diagnostic.mozc_dict.DICT_DIR, original_dir)
            best, _ = decode_tokens(diagnostic.plain_tokens("か"), lex)
            described = diagnostic.describe_path(best, lex, overlay, names)
            self.assertEqual(described["pieces"][0]["source_layer"], "user_overlay")
            self.assertEqual(described["pieces"][0]["cost"], 1)
            self.assertFalse(list(root.glob("*.pkl")))


if __name__ == "__main__":
    unittest.main()
