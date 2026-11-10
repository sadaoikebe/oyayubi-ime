"""Evaluate joint NICOLA decoding against Mozc's evaluation.tsv.

Run with ``python3 -m tools.wp0.run_mozc_eval``. Use
``--no-user-lexicon`` to evaluate the unmodified Mozc cache.
"""

from __future__ import annotations

import argparse
import csv
import pickle
import time

from .mozc_dict import CACHE, DICT_DIR, MozcLex, load_lex
from .table import load_table
from .token_viterbi import decode_tokens


def reverse_table(table: dict) -> dict[str, dict]:
    """Map each face to the lattice token produced by its key."""
    result = {}
    # Plain faces take precedence if a kana appears on multiple keys.
    for key, faces in table["keys"].items():
        kana = faces.get("plain")
        if kana:
            result.setdefault(kana, {"kind": "plain", "key": key, "kana": kana})
    for key, faces in table["keys"].items():
        shifts = {face: faces[face] for face in ("left", "right") if faces.get(face)}
        token = {"kind": "amb" if len(shifts) > 1 else "shift", "key": key, "faces": shifts}
        for kana in shifts.values():
            result.setdefault(kana, token)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-user-lexicon", action="store_true", help="use the unmodified Mozc cache"
    )
    args = parser.parse_args()

    if args.no_user_lexicon:
        if not CACHE.exists():
            parser.error(f"Mozc cache not found: {CACHE}; run once without --no-user-lexicon")
        # load_lex always overlays user entries; its cache is saved before that overlay.
        lex: MozcLex = pickle.loads(CACHE.read_bytes())
    else:
        lex = load_lex()
    reverse = reverse_table(load_table())
    total = evaluated = skipped = reading_matches = surface_matches = 0
    elapsed = 0.0
    with (DICT_DIR / "evaluation.tsv").open(encoding="utf-8", newline="") as f:
        for row in csv.reader(f, delimiter="\t"):
            if not row or row[0].startswith("#"):
                continue
            if len(row) < 3:
                raise ValueError(f"Malformed evaluation row {total + 1}: {row!r}")
            total += 1
            reading, expected = row[1:3]
            if any(kana not in reverse for kana in reading):
                skipped += 1
                continue
            tokens = [reverse[kana] for kana in reading]
            start = time.perf_counter()
            decoded, _ = decode_tokens(tokens, lex)
            elapsed += time.perf_counter() - start
            evaluated += 1
            reading_matches += decoded.reading == reading
            surface_matches += decoded.surface == expected

    print(f"User lexicon: {'off' if args.no_user_lexicon else 'on'}")
    print(f"Total rows: {total}")
    print(f"Evaluated: {evaluated}")
    print(f"Skipped (unencodable): {skipped}")
    if evaluated:
        print(f"Reading match: {reading_matches / evaluated * 100:.1f}% ({reading_matches}/{evaluated})")
        print(f"Surface match: {surface_matches / evaluated * 100:.1f}% ({surface_matches}/{evaluated})")
        print(f"Average ms/sentence: {elapsed * 1000 / evaluated:.3f}")
    else:
        print("No encodable rows; match rates and timing unavailable.")


if __name__ == "__main__":
    main()
