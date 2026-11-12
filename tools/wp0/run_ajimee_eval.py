"""Evaluate AJIMEE-Bench with known kana (una) and ambiguous thumbs (amb).

Run: python3 -m tools.wp0.run_ajimee_eval (fetch data with tools.wp0.fetch_ajimee).
Symbols outside the NICOLA table split the input and pass through unchanged.
Left context is reported as a subgroup but is not used for decoding.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import time

from .fetch_ajimee import DATA
from oyayubi.lattice.mozc_dict import BOS_ID, left_id_after, load_lex
from oyayubi.lattice.table import load_table, reverse_table
from oyayubi.lattice.token_viterbi import decode_tokens


def hiragana(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def chunks(reading: str, reverse: dict) -> list[tuple[bool, str]]:
    out = []
    current = ""
    for c in reading:
        if c in reverse:
            current += c
        else:
            if current:
                out.append((True, current))
                current = ""
            out.append((False, c))
    if current:
        out.append((True, current))
    return out


def convert(reading, mode, reverse, lex):
    surface, decoded_reading = "", ""
    elapsed = 0.0
    ambiguous = 0
    left_id = BOS_ID  # as ImeSession._refresh: a digit literal leaves a numeral context
    for is_kana, text in chunks(reading, reverse):
        if not is_kana:
            surface += text
            decoded_reading += text
            left_id = left_id_after(text, lex)
            continue
        tokens = ([reverse[c] for c in text] if mode == "amb" else
                  [{"kind": "plain", "key": "?", "kana": c} for c in text])
        ambiguous += sum(t["kind"] == "amb" for t in tokens)
        start = time.perf_counter()
        decoded, _ = decode_tokens(tokens, lex, left_id=left_id)
        elapsed += (time.perf_counter() - start) * 1000
        surface += decoded.surface
        decoded_reading += decoded.reading
    return surface, decoded_reading, elapsed, ambiguous


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--details", type=Path, help="write per-item results as JSONL")
    parser.add_argument("--no-user-lexicon", action="store_true")
    args = parser.parse_args()
    if not args.data.exists():
        parser.error("AJIMEE data missing; run python3 -m tools.wp0.fetch_ajimee")
    # Read JSON only: never import or execute downloaded evaluation code.
    items = json.loads(args.data.read_text(encoding="utf-8"))
    if not isinstance(items, list) or not items:
        parser.error("AJIMEE data must be a nonempty JSON array")
    lex = load_lex(user_lexicon=not args.no_user_lexicon)
    reverse = reverse_table(load_table())
    groups = {name: Counter() for name in ("all", "ctx", "noctx")}
    details, times, lengths, ambiguities = [], [], [], []
    for item in items:
        reading = hiragana(item["input"])
        golds = item["expected_output"]
        if not isinstance(golds, list) or not golds or not all(isinstance(g, str) for g in golds):
            raise ValueError("expected_output must be a nonempty list of strings")
        una, _, _, _ = convert(reading, "una", reverse, lex)
        amb, amb_reading, ms, k = convert(reading, "amb", reverse, lex)
        group = "ctx" if item.get("context_text") else "noctx"
        for name in ("all", group):
            counts = groups[name]
            counts["n"] += 1
            counts["una"] += una in golds
            counts["amb"] += amb in golds
            counts["amb_reading"] += amb_reading == reading
        times.append(ms)
        lengths.append(len(reading))
        ambiguities.append(k)
        details.append(dict(reading=reading, gold=golds, una=una, amb=amb,
                            amb_reading=amb_reading, ms=round(ms, 3), k=k,
                            context_text=item.get("context_text", "")))
    print(f"User lexicon: {'off' if args.no_user_lexicon else 'on'}")
    print("Exact match against any accepted output; punctuation passes through; context unused")
    for name, counts in groups.items():
        n = counts["n"]
        print(f"{name}: n={n}")
        if n:
            for metric in ("una", "amb", "amb_reading"):
                print(f"  {metric}: {counts[metric] / n * 100:.1f}% ({counts[metric]}/{n})")
    print(f"Length mean/max: {statistics.mean(lengths):.1f}/{max(lengths)}; "
          f"AmbShift mean/max: {statistics.mean(ambiguities):.1f}/{max(ambiguities)}")
    times.sort()
    print(f"amb decode ms mean/p95/max: {statistics.mean(times):.3f}/"
          f"{times[min(len(times) - 1, int(len(times) * .95))]:.3f}/{max(times):.3f}")
    if args.details:
        args.details.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in details),
                                encoding="utf-8")


if __name__ == "__main__":
    main()
