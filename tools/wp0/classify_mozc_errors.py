"""Local, bounded Mozc evaluation diagnostics (learning/LM are always off).

Run ``python3 -m tools.wp0.classify_mozc_errors --output /tmp/mozc-errors``.
The default cache and user overlay are the same as run_mozc_eval. No cache,
dictionary, learning file or existing report is rewritten. Use --from-text
to diagnose a text snapshot instead; cache/text equivalence is NOT assumed.

Only amb/una with the selected overlay setting are run, as requested for this
implementation. Re-run with --no-user-lexicon in a different output directory
for an overlay ablation. Linux fork workers enforce a wall deadline per row
(both conditions together) and an address-space limit. Partial measurements
survive worker termination. TSV output is a reference, never asserted truth.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import asdict
import difflib
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import pickle
import platform
try:
    import resource
except ImportError:  # Windows has no resource module / RLIMIT_AS
    resource = None
import subprocess
import sys
import time
import traceback
import unicodedata

from oyayubi.ime.bunsetsu import PAGE_SIZE, candidates
from . import mozc_dict
from .decode import DecodeResult, Piece
from .expand import is_literal_face
from .mozc_dict import (BOS_ID, CACHE, DICT_DIR, MAX_WORD_MORA, POS_KEEP, ROOT,
                        UNK_COST, UNK_LID, USER_LEX, Entry, MozcLex, apply_user_lex)
from .run_mozc_eval import criterion_passes, parse_criterion, reverse_table
from .table import DATA, load_table
from .token_viterbi import (BOUNDARY_RID, _Word, _end_cost, _word_cost,
                            decode_tokens, nbest_tokens)


PRIMARY_ORDER = (
    "input_excluded", "criterion_review", "reading_mismatch", "orthographic_variant",
    "generation_constraint", "search_pruning_suspected", "vocabulary_missing_suspected",
    "segmentation_suspected", "homophone_rank", "needs_review",
)
PRIMARY_NAMES = {
    "input_excluded": "入力対象外", "criterion_review": "比較基準要確認",
    "reading_mismatch": "読み違い", "orthographic_variant": "表記ゆれのみ",
    "generation_constraint": "探索・入力境界の制約",
    "search_pruning_suspected": "枝刈り疑い（探索制約）",
    "vocabulary_missing_suspected": "語彙不足疑い",
    "segmentation_suspected": "分割違い（推定）", "homophone_rank": "同音語順位",
    "needs_review": "未判定", "no_error": "観測上の不一致なし",
}
WIDTH_TABLE = str.maketrans({**{chr(i): chr(i - 0xFEE0) for i in range(0xFF01, 0xFF5F)},
                            "\u3000": " "})


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_rows(path: Path) -> list[dict]:
    """Keep physical line numbers, including repeated readings and comments."""
    digest = sha256(path)
    lines = path.read_bytes().splitlines(keepends=True)
    rows = []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream, delimiter="\t")
        previous = 0
        for fields in reader:
            first, previous = previous + 1, reader.line_num
            if not fields or fields[0].startswith("#"):
                continue
            if len(fields) < 5:
                raise ValueError(f"Malformed evaluation row at physical line {first}: {fields!r}")
            rows.append({
                "row_id": f"{digest}:{first}", "tsv_sha256": digest, "line_number": first,
                "content_sha256": hashlib.sha256(b"".join(lines[first - 1:previous])).hexdigest(),
                "fields": fields, "recorded_status": fields[0], "input_reading": fields[1],
                "recorded_surface": fields[2], "command": fields[3], "argument": fields[4],
                "recorded_version": fields[5] if len(fields) > 5 else None,
                "criterion": "not_applicable" if parse_criterion(fields[3]) is None
                else {"kind": parse_criterion(fields[3])[0],
                      "negative": parse_criterion(fields[3])[1],
                      "topn": parse_criterion(fields[3])[2]},
            })
    return rows


def load_snapshot(dictionary_dir: Path, cache: Path | None, user: Path | None):
    """Reuse loader semantics without its cache-writing side effect."""
    if cache is not None:
        lex = pickle.loads(cache.read_bytes())
    else:
        # These existing parsers use a module path. Restore it even on failure.
        original = mozc_dict.DICT_DIR
        mozc_dict.DICT_DIR = dictionary_dir
        try:
            by_reading, prefixes = mozc_dict._parse_dict_files()
            connect, size = mozc_dict._parse_connection()
        finally:
            mozc_dict.DICT_DIR = original
        lex = MozcLex(by_reading, prefixes, connect, size)
    changed = set()
    if user is not None:
        # Only a changed/new effective entry is attributed to the overlay.
        readings = {json.loads(line)["reading"] for line in user.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")}
        before = {entry_key(e) for reading in readings for e in lex.by_reading.get(reading, [])}
        apply_user_lex(lex, user)
        changed = {entry_key(e) for reading in readings for e in lex.by_reading.get(reading, [])} - before
    names = {int(i): name for line in (dictionary_dir / "id.def").read_text(
        encoding="utf-8").splitlines() for i, name in [line.split(None, 1)]}
    return lex, changed, names


def entry_key(e: Entry) -> tuple:
    return e.reading, e.surface, e.lid, e.rid, e.cost


def plain_tokens(reading: str) -> list[dict]:
    return [{"kind": "plain", "key": "?", "kana": ch} for ch in reading]


def ui_segments(pieces: list[Piece], names: dict[int, str]) -> list[tuple[int, int]]:
    """Same POS grouping as bunsetsu.segments_from, with the selected id.def."""
    groups = []
    for p in pieces:
        pos = names.get(int(p.pos), "").split(",")
        content = ((pos[0] in ("名詞", "動詞", "形容詞") and pos[1] not in ("接尾", "非自立"))
                   or pos[0] in ("副詞", "連体詞", "接続詞", "感動詞", "接頭詞"))
        if not groups or p.literal or p.learned or groups[-1][-1].literal or content:
            groups.append([])
        groups[-1].append(p)
    return [(g[0].start, g[-1].end) for g in groups]


def describe_path(decoded: DecodeResult, lex: MozcLex, overlay: set, names: dict) -> dict:
    pieces = []
    prev = BOUNDARY_RID
    word_total = connection_total = 0
    for p in decoded.pieces:
        e = Entry(p.surface, p.reading, int(p.pos), p.rid, p.cost)
        known = any(entry_key(e) == entry_key(other) for other in lex.by_reading.get(p.reading, []))
        w = _Word(p.start, p.end, e, unknown=not known and not p.literal, literal=p.literal)
        cost = 0 if p.literal else p.cost
        connection = _word_cost(lex, prev, w) - cost
        word_total += cost
        connection_total += connection
        pieces.append({**asdict(p), "lid": e.lid, "lid_name": names.get(e.lid),
                       "rid_name": names.get(e.rid), "connection_from_rid": prev,
                       "connection_cost": connection, "unknown": w.unknown,
                       "source_layer": ("literal" if p.literal else "unknown_backoff" if w.unknown
                                        else "user_overlay" if entry_key(e) in overlay else "mozc_base")})
        prev = w.rid
    eos = _end_cost(lex, prev)
    return {"reading": decoded.reading, "surface": decoded.surface, "score": decoded.score,
            "pieces": pieces, "word_cost": word_total, "connection_cost": connection_total + eos,
            "bos_cost": pieces[0]["connection_cost"] if pieces else 0, "eos_cost": eos,
            "word_boundaries": [p.end for p in decoded.pieces[:-1]],
            "ui_segments": ui_segments(decoded.pieces, names)}


def constrained_path(reading: str, surface: str, lex: MozcLex,
                     policy: str = "nbest") -> DecodeResult | None:
    """Exact DP on (input offset, output offset, RID), without a beam.

    nbest: dictionary only, <=16 tokens, cost <12000; engine: no high-cost
    exclusion; unrestricted: also allow long dictionary entries; backoff:
    engine arcs plus exactly the engine's single-character unknown fallback.
    No path here means only failure under this explicit policy.
    """
    states = [dict() for _ in range(len(reading) + 1)]
    states[0][(0, BOS_ID)] = (0, None, None)
    for i, layer in enumerate(states[:-1]):
        arcs = []
        end_limit = len(reading) if policy == "unrestricted" else min(len(reading), i + MAX_WORD_MORA)
        for end in range(i + 1, end_limit + 1):
            part = reading[i:end]
            if any(is_literal_face(ch) for ch in part):
                break  # Dictionary arcs cannot cross a literal boundary.
            for e in lex.by_reading.get(part, ()):
                if policy != "nbest" or e.cost < UNK_COST:
                    arcs.append((end, e))
        if policy == "backoff" and not lex.by_reading.get(reading[i]):
            ch = reading[i]
            arcs.append((i + 1, Entry(ch, ch, UNK_LID, UNK_LID, UNK_COST)))
        for (out, rid), (cost, _, _) in list(layer.items()):
            for end, e in arcs:
                if not surface.startswith(e.surface, out):
                    continue
                key = out + len(e.surface), e.rid
                score = cost + lex.transition(rid, e.lid) + e.cost
                if key not in states[end] or score < states[end][key][0]:
                    states[end][key] = (score, (i, out, rid), e)
    finals = [(cost + lex.transition(rid, BOS_ID), rid) for (out, rid), (cost, _, _) in states[-1].items()
              if out == len(surface)]
    if not finals:
        return None
    score, rid = min(finals)
    i, out = len(reading), len(surface)
    pieces = []
    while i:
        _, previous, e = states[i][(out, rid)]
        start, old_out, old_rid = previous
        pieces.append(Piece(e.surface, e.reading, str(e.lid), e.cost, start, i, e.rid))
        i, out, rid = start, old_out, old_rid
    return DecodeResult(reading, surface, score, list(reversed(pieces)))


def normalization(a: str, b: str) -> dict:
    return {"raw": a == b,
            "nfc": unicodedata.normalize("NFC", a) == unicodedata.normalize("NFC", b),
            "ascii_width": a.translate(WIDTH_TABLE) == b.translate(WIDTH_TABLE),
            "nfkc_reference_only": unicodedata.normalize("NFKC", a) == unicodedata.normalize("NFKC", b)}


def differences(actual: str, target: str, pieces: list[Piece]) -> list[dict]:
    offsets, offset = [], 0
    for p in pieces:
        offsets.append((offset, offset + len(p.surface), p.start, p.end))
        offset += len(p.surface)
    changes = []
    for kind, a, b, c, d in difflib.SequenceMatcher(None, actual, target, autojunk=False).get_opcodes():
        if kind == "equal":
            continue
        changes.append({"kind": kind, "actual_range": [a, b], "target_range": [c, d],
                        "actual": actual[a:b], "target": target[c:d],
                        "overlapping_piece_reading_spans": [[s, e] for x, y, s, e in offsets
                                                            if (x < b and y > a) or (a == b and x <= a <= y)],
                        "boundary_is_inferred": True})
    return changes


def target_diagnostic(reading: str, target: str, decoded: DecodeResult, tokens: list[dict],
                      exact: list[DecodeResult], lex: MozcLex, overlay: set, names: dict) -> dict:
    surfaces = [d.surface for d in exact]
    rank = surfaces.index(target) + 1 if target in surfaces else None
    prepended = list(dict.fromkeys([decoded.surface] + surfaces))[:30]
    path = constrained_path(reading, target, lex)
    engine = path or constrained_path(reading, target, lex, "engine")
    unrestricted = engine or constrained_path(reading, target, lex, "unrestricted")
    backoff = engine or constrained_path(reading, target, lex, "backoff")
    reasons = []
    if not engine and unrestricted:
        reasons.append("word_length_limit")
    if not path and engine:
        reasons.append("nbest_high_cost_exclusion")
    prefix = 0
    while prefix < min(len(decoded.surface), len(target)) and decoded.surface[prefix] == target[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < min(len(decoded.surface), len(target)) - prefix
           and decoded.surface[-suffix - 1] == target[-suffix - 1]):
        suffix += 1
    info = {
        "target": target, "target_source": "reference_not_human_verified",
        "direct_whole_string_entry": any(e.surface == target for e in lex.by_reading.get(reading, [])),
        "direct_entry_is_not_a_word_oov_test": True,
        "dictionary_path_nbest_policy": path is not None,
        "dictionary_path_engine_policy": engine is not None,
        "dictionary_path_unrestricted_length": unrestricted is not None,
        "path_with_engine_unknown_backoff": backoff is not None,
        "rank_exact": rank, "rank_status": "found" if rank else "rank_gt_30_or_unreachable",
        "rank_1best_prepended": prepended.index(target) + 1 if target in prepended else None,
        "sentence_at": {str(k): rank is not None and rank <= k for k in (1, 3, 8, 30)},
        "generation_reasons": reasons, "normalization": normalization(decoded.surface, target),
        "common_prefix_characters": prefix, "common_suffix_characters": suffix,
        "differences": differences(decoded.surface, target, decoded.pieces),
        "ui_candidates": [],
    }
    chosen = engine or unrestricted or backoff
    if chosen:
        detail = describe_path(chosen, lex, overlay, names)
        current = describe_path(decoded, lex, overlay, names)
        info["target_path"] = detail
        info["cost_delta"] = {key: detail[key] - current[key]
                              for key in ("score", "word_cost", "connection_cost", "bos_cost", "eos_cost")}
        comparable = decoded.reading == reading
        info["boundaries_comparable"] = comparable
        info["word_boundaries_differ"] = comparable and detail["word_boundaries"] != current["word_boundaries"]
        info["ui_boundaries_differ"] = comparable and detail["ui_segments"] != current["ui_segments"]
        info["search_pruning_suspected"] = bool(engine and comparable and engine.score < decoded.score)
        info["path_entry_lookups"] = [
            {"reading": p.reading, "surface": p.surface,
             "direct_entry": any(e.surface == p.surface for e in lex.by_reading.get(p.reading, [])),
             "reading_span": [p.start, p.end], "word_identity_confirmed": False}
            for p in chosen.pieces]
        if comparable and decoded.surface != target:
            for start, end in current["ui_segments"]:
                target_pieces = [p for p in chosen.pieces if start <= p.start and p.end <= end]
                if not target_pieces or target_pieces[0].start != start or target_pieces[-1].end != end:
                    info["ui_candidates"].append({"span": [start, end],
                                                   "status": "target_crosses_current_segment_boundary"})
                    continue
                wanted = "".join(p.surface for p in target_pieces)
                selected = "".join(p.surface for p in decoded.pieces if start <= p.start and p.end <= end)
                if wanted == selected:
                    continue
                left = next((p.rid for p in reversed(decoded.pieces) if p.end == start), BOS_ID)
                right = next((int(p.pos) for p in decoded.pieces if p.start == end), BOS_ID)
                ui_rank = None
                for number, candidate in enumerate(candidates(tokens[start:end], lex, left, right), 1):
                    if candidate.surface == wanted and candidate.reading == reading[start:end]:
                        ui_rank = number
                        break
                    if number == 30:
                        break
                info["ui_candidates"].append({"span": [start, end], "target": wanted, "rank": ui_rank,
                                               "page": (ui_rank - 1) // PAGE_SIZE + 1 if ui_rank else None,
                                               "status": "found" if ui_rank else "rank_gt_30_or_unreachable",
                                               "page_size": PAGE_SIZE, "search_limit": 30})
    return info


def diagnose_condition(row: dict, mode: str, reverse: dict, lex: MozcLex,
                       overlay: set, names: dict, emit) -> dict:
    reading = row["input_reading"]
    tokens = plain_tokens(reading) if mode == "una" else [reverse[ch] for ch in reading]
    best, _ = decode_tokens(tokens, lex)
    criterion = parse_criterion(row["command"])
    result = {"status": "partial", **describe_path(best, lex, overlay, names),
              "reading_match": best.reading == reading,
              "surface_match": best.surface == row["recorded_surface"], "command_pass": None,
              "command_status": "not_applicable" if criterion is None else "pending",
              "command_candidate_policy": "rid_pruned_1best_prepended_distinct_sentence_nbest",
              "diagnostics": {}}
    if criterion is not None and (criterion[0] == "Match" or criterion[2] == 1):
        result.update(command_pass=criterion_passes(criterion, row["argument"], best.surface, [best.surface]),
                      command_status="evaluated")
    emit(mode, result)
    exact, stats = nbest_tokens(tokens, lex, k=30)
    result["nbest"] = {"policy": "exact_distinct_surfaces_unknown_and_cost_ge_12000_excluded",
                       "limit": 30, "stats": stats,
                       "candidates": [{"surface": d.surface, "reading": d.reading, "score": d.score}
                                      for d in exact]}
    evaluation_candidates = list(dict.fromkeys([best.surface] + [d.surface for d in exact]))
    if criterion is not None and result["command_pass"] is None:
        topn = criterion[2]
        if topn <= 30 or row["argument"] in evaluation_candidates[:topn] or len(exact) < 30:
            result.update(command_pass=criterion_passes(criterion, row["argument"], best.surface,
                                                       evaluation_candidates), command_status="evaluated")
    emit(mode, result)
    targets = {"recorded": row["recorded_surface"]}
    if criterion and criterion[0] == "Expected" and not criterion[1]:
        targets["positive_expected"] = row["argument"]
    for source, target in targets.items():
        # Reuse identical target diagnostics, preserving each source name.
        same = next((d for d in result["diagnostics"].values() if d["target"] == target), None)
        result["diagnostics"][source] = same or target_diagnostic(
            reading, target, best, tokens, exact, lex, overlay, names)
        emit(mode, result)
    if criterion is not None and result["command_pass"] is None:
        # Evaluation's Expected 99/999 must not silently become Expected 30.
        # Only this command check expands further, within the same row budget.
        extended, _ = nbest_tokens(tokens, lex, k=criterion[2])
        evaluation_candidates = list(dict.fromkeys([best.surface] + [d.surface for d in extended]))
        result.update(command_pass=criterion_passes(criterion, row["argument"], best.surface,
                                                   evaluation_candidates), command_status="evaluated")
    result.update(status="complete", command_candidates=evaluation_candidates[:criterion[2]] if criterion else [])
    emit(mode, result)
    return result


def _worker(connection, lex, overlay, names, reverse, memory_mb):
    if resource is not None:  # memory cap is unavailable on Windows
        resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024,) * 2)
    try:
        while True:
            row = connection.recv()
            if row is None:
                return
            try:
                for mode in ("amb", "una"):
                    diagnose_condition(row, mode, reverse, lex, overlay, names,
                                       lambda key, value: connection.send(("progress", key, value)))
                connection.send(("done",))
            except MemoryError:
                connection.send(("unknown_resource_limit",))
                return
            except Exception:
                connection.send(("error", traceback.format_exc()))
                return
    finally:
        connection.close()


def classify(row: dict) -> dict:
    """One primary per row; secondary labels are deliberately overlapping."""
    labels, reasons = set(), []
    if row["state"] == "input_excluded":
        return {"primary": "input_excluded", "labels": ["input_excluded"],
                "needs_review": False, "confidence": "high", "review_reasons": []}
    amb, una = (row["conditions"].get(mode, {}) for mode in ("amb", "una"))
    criterion = parse_criterion(row["command"])
    if row["state"] != "complete":
        labels.update((row["state"], "needs_review"))
        reasons.append(row["state"])
    if amb.get("reading_match") is False:
        labels.add("reading_mismatch")
    raw_mismatch = amb.get("surface_match") is False
    failed = amb.get("command_pass") is False
    reference_conflict = bool(criterion and criterion[0] == "Expected" and not criterion[1]
                              and row["argument"] != row["recorded_surface"])
    if criterion is None or reference_conflict or ((raw_mismatch or failed) and
                                                  (criterion[0] != "Expected" or criterion[1])):
        labels.add("criterion_review")
        reasons.append("command_does_not_confirm_recorded_full_surface")
    if failed and not raw_mismatch:
        labels.add("criterion_review")
        reasons.append("recorded_surface_match_but_command_failed")
    diag = amb.get("diagnostics", {}).get("positive_expected",
                                          amb.get("diagnostics", {}).get("recorded", {}))
    if (raw_mismatch or failed) and amb.get("surface") != diag.get("target"):
        norm = diag.get("normalization", {})
        if not norm.get("raw") and (norm.get("nfc") or norm.get("ascii_width")):
            labels.add("orthographic_variant")
        if diag.get("generation_reasons"):
            labels.add("generation_constraint")
        if diag.get("search_pruning_suspected"):
            labels.add("search_pruning_suspected")
        if diag.get("word_boundaries_differ"):
            labels.add("segmentation_suspected")
        if diag.get("dictionary_path_engine_policy") and amb.get("reading_match"):
            labels.add("homophone_rank")
        reasons.append("target_intent_and_word_identity_not_human_verified")
        if not diag.get("dictionary_path_unrestricted_length"):
            labels.add("needs_review")
            reasons.append("no_confirmed_word_reading_pair_for_oov_claim")
    for condition in (amb, una):
        for p in condition.get("pieces", []):
            pos = p.get("lid_name") or ""
            if "固有名詞" in pos:
                labels.add("proper_noun_pos")
                if "人名" in pos:
                    labels.add("person_name_pos")
                if "地域" in pos:
                    labels.add("place_name_pos")
            if "数" in pos or any(ch.isdecimal() for ch in p["surface"]):
                labels.add("numeric_pos_or_pattern")
    if row["state"] == "complete" and not raw_mismatch and not failed and amb.get("reading_match"):
        primary = "criterion_review" if "criterion_review" in labels else "no_error"
    else:
        primary = next((label for label in PRIMARY_ORDER if label in labels), "needs_review")
    if primary == "needs_review":
        labels.add("needs_review")
    return {"primary": primary, "labels": sorted(labels), "needs_review": bool(reasons),
            "confidence": "high" if primary == "no_error" else "medium" if primary == "reading_mismatch" else "low",
            "review_reasons": reasons,
            "amb_to_una": {"recorded_surface_recovered": raw_mismatch and una.get("surface_match") is True,
                           "una_target_reachable": una.get("diagnostics", {}).get("positive_expected",
                                una.get("diagnostics", {}).get("recorded", {})).get("dictionary_path_engine_policy")}}


def run_rows(rows, lex, overlay, names, reverse, timeout=10.0, memory_mb=2048):
    # fork lets the worker inherit the caller's lexicon (and, in tests, a
    # patched diagnose_condition); elsewhere fall back to the platform
    # default context, where the worker re-imports and receives pickled args.
    ctx = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else mp.get_context()
    process = connection = None

    def stop():
        if process is not None:
            process.terminate()
            process.join()
            connection.close()

    try:
        for source in rows:
            row = {**source, "conditions": {}}
            row["unencodable"] = [{"position": i, "character": ch}
                                  for i, ch in enumerate(row["input_reading"]) if ch not in reverse]
            start = time.monotonic()
            if row["unencodable"]:
                row["state"] = "input_excluded"
            else:
                if process is None:
                    connection, child = ctx.Pipe()
                    process = ctx.Process(target=_worker, args=(child, lex, overlay, names, reverse, memory_mb))
                    process.start()
                    child.close()
                connection.send(row)
                row["state"] = "complete"
                while True:
                    remaining = timeout - (time.monotonic() - start)
                    if remaining <= 0 or not connection.poll(max(0, remaining)):
                        row["state"] = "unknown_timeout"
                        stop()
                        process = connection = None
                        break
                    message = connection.recv()
                    if message[0] == "progress":
                        row["conditions"][message[1]] = message[2]
                    elif message[0] == "done":
                        break
                    elif message[0] == "error":
                        raise RuntimeError(message[1])
                    else:
                        row["state"] = message[0]
                        stop()
                        process = connection = None
                        break
                if row["state"] != "complete":
                    for mode in ("amb", "una"):
                        condition = row["conditions"].setdefault(mode, {})
                        if condition.get("status") != "complete":
                            condition["status"] = row["state"]
                            if condition.get("command_status") == "pending":
                                condition["command_status"] = row["state"]
            row["elapsed_seconds"] = round(time.monotonic() - start, 6)
            row["automatic_classification"] = classify(row)
            yield row
    finally:
        stop()


def condition_differences(rows, metric):
    result = {"improved": [], "worsened": [], "unchanged": [], "unknown": []}
    for row in rows:
        if row["state"] == "input_excluded":
            continue
        before, after = [row["conditions"].get(mode, {}).get(metric) for mode in ("amb", "una")]
        key = ("unknown" if before is None or after is None else "improved" if after and not before
               else "worsened" if before and not after else "unchanged")
        result[key].append(row["row_id"])
    return result


def problem_patterns(row, primary, amb, diag):
    """Group comparable changed spans, without treating inferred spans as words."""
    if primary == "criterion_review":
        return [f"criterion:{row['command']}|recorded_status:{row['recorded_status']}"]
    if primary == "reading_mismatch":
        return sorted({f"reading:{row['input_reading'][a:b]}=>{amb.get('reading', '')[c:d]}"
                       for kind, a, b, c, d in difflib.SequenceMatcher(
                           None, row["input_reading"], amb.get("reading", ""), autojunk=False).get_opcodes()
                       if kind != "equal"})
    if diag.get("boundaries_comparable"):
        current, target = amb["pieces"], diag["target_path"]["pieces"]
        boundaries = sorted(({0} | {p["end"] for p in current}) & ({0} | {p["end"] for p in target}))
        patterns = set()
        for start, end in zip(boundaries, boundaries[1:]):
            before = "".join(p["surface"] for p in current if start <= p["start"] and p["end"] <= end)
            after = "".join(p["surface"] for p in target if start <= p["start"] and p["end"] <= end)
            if before != after:
                patterns.add(f"{primary}:inferred_span:{row['input_reading'][start:end]}|{before}=>{after}")
        if patterns:
            return sorted(patterns)
    return [f"{primary}:sentence_pattern:{row['input_reading']}|{amb.get('surface', '?')}=>{diag.get('target', '?')}"]


def work_items(rows):
    grouped = {}
    for row in rows:
        classification = row["automatic_classification"]
        primary = classification["primary"]
        if primary in ("no_error", "input_excluded"):
            continue
        amb = row["conditions"].get("amb", {})
        diag = amb.get("diagnostics", {}).get("positive_expected", amb.get("diagnostics", {}).get("recorded", {}))
        # A row can affect multiple patterns; a pattern counts each row once.
        for pattern in problem_patterns(row, primary, amb, diag):
            item = grouped.setdefault(pattern, {"pattern": pattern, "primary": primary, "row_ids": [],
                                                "ranks": [], "daily_frequency": "未確認",
                                                "effort": "未見積", "decision": "未採否", "reviewer": "未確認"})
            item["row_ids"].append(row["row_id"])
            item["ranks"].append(diag.get("rank_exact") or diag.get("rank_status", "unknown_not_measured"))
    actions = {"criterion_review": "記録表記とコマンドの意図を確認（Not引数を追加語にしない）",
               "reading_mismatch": "左右解決とuna対照を確認",
               "orthographic_variant": "明示同値規則と許容表記を確認",
               "generation_constraint": "長さ・高コスト除外・入力境界をエンジン課題として確認",
               "search_pruning_suspected": "厳密経路とRID枝刈りを比較",
               "segmentation_suspected": "推定単語境界とUI文節境界を確認",
               "homophone_rank": "単語・連接の差を確認し小幅コスト調整を検討"}
    items = sorted(grouped.values(), key=lambda item: (-len(item["row_ids"]), item["pattern"]))
    for item in items:
        item["affected_rows"] = len(item["row_ids"])
        item["suggested_action"] = actions.get(item["primary"], "読み・語の同定と到達性を人手確認")
    return items


def write_reports(output: Path, rows: list[dict], manifest: dict):
    output.mkdir(parents=True, exist_ok=True)
    items = work_items(rows)
    primary = Counter(row["automatic_classification"]["primary"] for row in rows)
    assert sum(primary.values()) == len(rows)
    deltas = {metric: condition_differences(rows, metric)
              for metric in ("reading_match", "surface_match", "command_pass")}
    manifest.update(primary_counts=dict(primary), condition_differences=deltas,
                    ledger_rows=len(rows), encodable_rows=sum(r["state"] != "input_excluded" for r in rows),
                    states=dict(Counter(r["state"] for r in rows)))
    baseline = {"amb": {"reading_match": 549, "surface_match": 458, "command_pass": 380},
                "una": {"surface_match": 496}}
    manifest["historical_baseline"] = baseline
    manifest["measured_conditions"] = {
        mode: {metric: {"true": sum(r["conditions"].get(mode, {}).get(metric) is True for r in rows),
                        "measured": sum(r["conditions"].get(mode, {}).get(metric) is not None for r in rows)}
               for metric in ("reading_match", "surface_match", "command_pass")}
        for mode in ("amb", "una")}
    (output / "rows.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    (output / "run.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    columns = ["pattern", "primary", "affected_rows", "row_ids", "daily_frequency", "ranks",
               "suggested_action", "effort", "decision", "reviewer"]
    with (output / "worklist.tsv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, delimiter="\t")
        writer.writeheader()
        for item in items:
            writer.writerow({**item, "row_ids": json.dumps(item["row_ids"], ensure_ascii=False),
                             "ranks": json.dumps(item["ranks"], ensure_ascii=False)})
    lines = ["# Mozc 誤り分類（自動診断）", "", "learning=off / LM=off / amb, una の2条件。",
             f"手書き辞書: {'あり' if manifest['settings']['user_lexicon'] else 'なし'}。",
             f"入力台帳: {len(rows)} / 入力可能: {manifest['encodable_rows']} / "
             f"入力不能: {primary['input_excluded']}。",
             f"所要時間: {manifest['elapsed_seconds']:.3f} 秒（ロード・ハッシュ計算を含む）。", "",
             "|条件|読み一致 / 測定済|記録表記一致 / 測定済|コマンド合格 / 判定済|コマンド対象外|",
             "|---|---:|---:|---:|---:|"]
    for mode in ("amb", "una"):
        values = [r["conditions"].get(mode, {}) for r in rows if r["state"] != "input_excluded"]
        counts = [f"{sum(v.get(key) is True for v in values)} / {sum(v.get(key) is not None for v in values)}"
                  for key in ("reading_match", "surface_match", "command_pass")]
        lines.append(f"|{mode}|" + "|".join(counts) + f"|{sum(v.get('command_status') == 'not_applicable' for v in values)}|")
    lines += ["", "主分類は台帳全行に一つ。観測上の不一致なしも分母に含める。", "",
              "|主分類|行数|独立問題パターン数|要確認|", "|---|---:|---:|---:|"]
    for label in (*PRIMARY_ORDER, "no_error"):
        lines.append(f"|{PRIMARY_NAMES[label]} (`{label}`)|{primary[label]}|"
                     f"{sum(i['primary'] == label for i in items)}|"
                     f"{sum(r['automatic_classification']['primary'] == label and r['automatic_classification']['needs_review'] for r in rows)}|")
    lines += [f"|合計|{sum(primary.values())}|||", "",
              f"未判定（主分類）: {primary['needs_review']}。"
              f"needs_review 補助タグ: {sum('needs_review' in r['automatic_classification']['labels'] for r in rows)}。"
              f"要人手確認（全分類）: {sum(r['automatic_classification']['needs_review'] for r in rows)}。",
              f"unknown_timeout: {sum(r['state'] == 'unknown_timeout' for r in rows)} / "
              f"unknown_resource_limit: {sum(r['state'] == 'unknown_resource_limit' for r in rows)}。", "",
              "|amb → una|改善|悪化|同じ|未測定・対象外|", "|---|---:|---:|---:|---:|"]
    for metric, delta in deltas.items():
        lines.append(f"|{metric}|" + "|".join(str(len(delta[key])) for key in delta) + "|")
    lines += ["", "改善・悪化の行IDは run.json の condition_differences。手書き辞書のあり/なし差分は今回未測定。",
              "Expected N は既存評価と同じ、1-bestを先頭に追加した異なる全文候補の上位Nによる近似。",
              "診断順位は未知補完・単語コスト12000以上を除く厳密全文n-best。UI候補の順位・ページとは別。",
              "上位30件未発見は rank_gt_30_or_unreachable。全文の単独エントリ不在はOOV判定に使わない。",
              "NFCとASCII全半角のみ主分類の同値規則。NFKCは参考のみ。目的表記・分割・語の同定は人手確認前。",
              "worklist の推定区間は単語同定前の問題パターン。影響行数はパターン内で重複なし、パターン間では重複あり。",
              "既存記録: amb 読み549/594、表記458/594、コマンド380/592、una表記496/594。"]
    for mode, metrics in baseline.items():
        lines.append(f"既存記録との差 ({mode}): " + ", ".join(
            f"{metric} {manifest['measured_conditions'][mode][metric]['true'] - value:+d}"
            for metric, value in metrics.items()) + "。未測定がある場合は比較未確定。")
    lines += ["過去記録との差は辞書改善効果とはみなさず、現行評価コマンドによる再計測とも照合する。",
              "キャッシュ使用時はそのハッシュも保存。元テキストとの一致は未検証。", "",
              "補助タグ（重複あり）:", ""]
    tags = Counter(tag for r in rows for tag in r["automatic_classification"]["labels"])
    lines.extend(f"- {tag}: {count}" for tag, count in sorted(tags.items()))
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new/empty report directory")
    parser.add_argument("--tsv", type=Path)
    parser.add_argument("--dictionary-dir", type=Path, default=DICT_DIR)
    parser.add_argument("--cache", type=Path, help="explicit trusted local pickle; recorded in manifest")
    parser.add_argument("--from-text", action="store_true", help="parse text without reading/writing any cache")
    parser.add_argument("--user-lexicon", type=Path, default=USER_LEX)
    parser.add_argument("--no-user-lexicon", action="store_true")
    parser.add_argument("--table", type=Path, default=DATA)
    parser.add_argument("--row-timeout", type=float, default=10.0)
    parser.add_argument("--memory-mb", type=int, default=2048)
    parser.add_argument("--limit", type=int, help="diagnostic subset, marked in manifest")
    args = parser.parse_args()
    if args.row_timeout <= 0 or args.memory_mb <= 0 or (args.limit is not None and args.limit <= 0):
        parser.error("timeout, memory and limit must be positive")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("--output must be new or empty; existing reports are not overwritten")
    if args.from_text and args.cache:
        parser.error("choose either --from-text or --cache")
    start = time.monotonic()
    tsv = args.tsv or args.dictionary_dir / "evaluation.tsv"
    cache = None if args.from_text else args.cache or (CACHE if args.dictionary_dir == DICT_DIR and CACHE.exists() else None)
    user = None if args.no_user_lexicon else args.user_lexicon
    # Exactly the files required by the selected snapshot, not an open glob.
    inputs = [tsv, args.table, args.dictionary_dir / "id.def", args.dictionary_dir / "suffix.txt",
              args.dictionary_dir / "connection_single_column.txt",
              *(args.dictionary_dir / f"dictionary{i:02}.txt" for i in range(10))]
    if user:
        inputs.append(user)
    if cache:
        inputs.append(cache)
    hashes = {str(path.resolve()): sha256(path) for path in inputs}
    engine_files = [Path(__file__), ROOT / "tools/wp0/mozc_dict.py", ROOT / "tools/wp0/token_viterbi.py",
                    ROOT / "tools/wp0/run_mozc_eval.py", ROOT / "tools/wp0/expand.py",
                    ROOT / "tools/wp0/decode.py", ROOT / "tools/wp0/table.py", ROOT / "oyayubi/ime/bunsetsu.py"]
    git = lambda *parts: subprocess.check_output(["git", *parts], cwd=ROOT)
    manifest = {"format_version": 1, "python": platform.python_version(),
                "code_head": git("rev-parse", "HEAD").decode().strip(),
                "dirty_diff_sha256": hashlib.sha256(git("diff", "HEAD", "--binary")).hexdigest(),
                "git_status": git("status", "--porcelain").decode().splitlines(),
                "engine_sha256": {str(path.relative_to(ROOT)): sha256(path) for path in engine_files},
                "inputs_sha256": hashes, "upstream_commit": "unknown",
                "lexicon_source": "cache_text_equivalence_unverified" if cache else "text",
                "settings": {"conditions": ["amb", "una"], "user_lexicon": user is not None,
                             "learning": False, "lm": False, "row_timeout_seconds": args.row_timeout,
                             "memory_mb": args.memory_mb, "nbest_diagnostic_limit": 30,
                             "sentence_thresholds": [1, 3, 8, 30], "max_word_tokens": MAX_WORD_MORA,
                             "pos_keep": POS_KEEP, "ui_candidate_limit": 30, "limit": args.limit}}
    rows = read_rows(tsv)
    manifest["source_rows"] = len(rows)
    if args.limit:
        rows = rows[:args.limit]
    lex, overlay, names = load_snapshot(args.dictionary_dir, cache, user)
    reverse = reverse_table(load_table(args.table))
    results = []
    for i, row in enumerate(run_rows(rows, lex, overlay, names, reverse, args.row_timeout, args.memory_mb), 1):
        results.append(row)
        if i % 50 == 0 or row["state"].startswith("unknown"):
            print(f"{i}/{len(rows)} {row['state']} ({time.monotonic() - start:.1f}s)", file=sys.stderr)
    manifest["elapsed_seconds"] = round(time.monotonic() - start, 6)
    write_reports(args.output, results, manifest)
    print(f"{args.output / 'summary.md'}: {len(results)} rows, {manifest['elapsed_seconds']:.3f}s")


if __name__ == "__main__":
    main()
