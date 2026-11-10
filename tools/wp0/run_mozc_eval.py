"""Evaluate joint NICOLA decoding against Mozc's evaluation.tsv.

Run with ``python3 -m tools.wp0.run_mozc_eval``. Use
``--no-user-lexicon`` to evaluate the unmodified Mozc cache.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import re
import time

from .mozc_dict import DICT_DIR, load_lex
from .expand import is_literal_face
from .table import load_table
from .token_viterbi import decode_tokens, nbest_tokens


def parse_criterion(command: str) -> tuple[str, bool, int] | None:
    """Expected N is approximated by the top N distinct whole-string candidates.

    Mozc uses segment candidate ranks; this decoder cannot reproduce that yet.
    Match tests a substring of 1-best. Not negates the result.
    """
    if not command.startswith("Conversion "):
        return None
    match = re.fullmatch(r"Conversion (Not )?(Expected|Match)(?: ([1-9][0-9]*))?", command)
    if match is None:
        raise ValueError(f"Unsupported conversion criterion: {command}")
    negative, kind, rank = match.groups()
    return kind, bool(negative), int(rank or 1)


def criterion_passes(criterion, argument: str, best: str, candidates: list[str]) -> bool:
    kind, negative, topn = criterion
    hit = argument in best if kind == "Match" else argument in candidates[:topn]
    return not hit if negative else hit


def reverse_table(table: dict) -> dict[str, dict]:
    """Map each face to the lattice token produced by its key."""
    result = {}
    # Plain faces take precedence if a kana appears on multiple keys.
    for key, faces in table["keys"].items():
        kana = faces.get("plain")
        # Evaluation encodes kana readings only; punctuation is handled by the
        # IME as a literal boundary, not as a kana dictionary arc.
        if kana and not is_literal_face(kana):
            result.setdefault(kana, {"kind": "plain", "key": key, "kana": kana})
    for key, faces in table["keys"].items():
        shifts = {face: faces[face] for face in ("left", "right") if faces.get(face)}
        token = {"kind": "amb" if len(shifts) > 1 else "shift", "key": key, "faces": shifts}
        for kana in shifts.values():
            if not is_literal_face(kana):
                result.setdefault(kana, token)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-user-lexicon", action="store_true", help="use the unmodified Mozc cache"
    )
    parser.add_argument("--unambiguous", action="store_true", help="use known kana (una ceiling)")
    parser.add_argument("--details", type=Path, help="write per-row PASS/FAIL/SKIP as JSONL")
    args = parser.parse_args()

    lex = load_lex(user_lexicon=not args.no_user_lexicon)
    reverse = reverse_table(load_table())
    total = evaluated = skipped = reading_matches = surface_matches = 0
    criteria = Counter()
    groups: dict[str, Counter] = {}
    details = []
    elapsed = 0.0
    with (DICT_DIR / "evaluation.tsv").open(encoding="utf-8", newline="") as f:
        for row in csv.reader(f, delimiter="\t"):
            if not row or row[0].startswith("#"):
                continue
            if len(row) < 5:
                raise ValueError(f"Malformed evaluation row {total + 1}: {row!r}")
            total += 1
            reading, expected = row[1:3]
            if any(kana not in reverse for kana in reading):
                skipped += 1
                continue
            tokens = ([{"kind": "plain", "key": "?", "kana": kana} for kana in reading]
                      if args.unambiguous else [reverse[kana] for kana in reading])
            start = time.perf_counter()
            decoded, _ = decode_tokens(tokens, lex)
            elapsed += time.perf_counter() - start
            evaluated += 1
            reading_matches += decoded.reading == reading
            surface_matches += decoded.surface == expected
            criterion = parse_criterion(row[3])
            detail = {"reading": reading, "command": row[3], "argument": row[4],
                      "surface": decoded.surface, "result": "SKIP"}
            if criterion is None:
                criteria["skipped"] += 1
            else:
                kind, negative, topn = criterion
                candidates = [decoded.surface]
                if kind == "Expected" and topn > 1:
                    results, _ = nbest_tokens(tokens, lex, k=topn)
                    # The two paths deliberately use different pruning keys.
                    candidates = list(dict.fromkeys(candidates + [d.surface for d in results]))[:topn]
                passed = criterion_passes(criterion, row[4], decoded.surface, candidates)
                mozc_passed = row[0].startswith("OK")
                label = f"{'Not ' if negative else ''}{kind} {topn}"
                group = groups.setdefault(label, Counter())
                for counts in (criteria, group):
                    counts["n"] += 1
                    counts["pass"] += passed
                    counts["mozc_pass"] += mozc_passed
                detail.update(result="PASS" if passed else "FAIL", candidates=candidates,
                              mozc_pass=mozc_passed)
            details.append(detail)

    print(f"User lexicon: {'off' if args.no_user_lexicon else 'on'}")
    print(f"Tokens: {'una (known kana)' if args.unambiguous else 'amb (L/R ambiguous)'}")
    print(f"Total rows: {total}")
    print(f"Evaluated: {evaluated}")
    print(f"Skipped (unencodable): {skipped}")
    if evaluated:
        print(f"Reading match: {reading_matches / evaluated * 100:.1f}% ({reading_matches}/{evaluated})")
        print(f"Surface match: {surface_matches / evaluated * 100:.1f}% ({surface_matches}/{evaluated})")
        print(f"Average ms/sentence: {elapsed * 1000 / evaluated:.3f}")
    else:
        print("No encodable rows; match rates and timing unavailable.")
    print("Criteria: Expected N = top N distinct whole-string candidates (approximation); "
          "Match = substring of 1-best; Not = negation")
    if criteria["n"]:
        n = criteria["n"]
        print(f"Command pass: {criteria['pass'] / n * 100:.1f}% ({criteria['pass']}/{n}); "
              f"FAIL {n - criteria['pass']}")
        print(f"Mozc recorded pass: {criteria['mozc_pass'] / n * 100:.1f}% "
              f"({criteria['mozc_pass']}/{n})")
    print(f"Skipped criteria (non-Conversion): {criteria['skipped']}")
    for label, counts in sorted(groups.items()):
        print(f"  {label}: PASS {counts['pass']} / FAIL {counts['n'] - counts['pass']} "
              f"(n={counts['n']})")
    if args.details:
        args.details.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in details),
                                encoding="utf-8")


if __name__ == "__main__":
    main()
