"""Measure amb n-best reach on AJIMEE (no learning or LM).

Run: python3 -m tools.wp0.run_ajimee_nbest_eval
sentence@k ranks whole sentences by summed chunk costs. chunk_oracle@k uses
the Cartesian product of each chunk's top k, matching Fable E5's metric;
it is an oracle upper bound, not the top k whole sentences.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from heapq import heappop, heappush
import json
from pathlib import Path
import statistics
import time

from .fetch_ajimee import DATA
from yuragi.lattice.mozc_dict import BOS_ID, left_id_after, load_lex
from .run_ajimee_eval import chunks, hiragana
from yuragi.lattice.table import load_table, reverse_table
from yuragi.lattice.token_viterbi import decode_tokens, nbest_tokens


def chunk_reaches(per_chunk: list[list[tuple[str, int]]], gold: str, k: int) -> bool:
    """Match a gold against the chunk product without materializing it."""
    offsets = {0}
    for candidates in per_chunk:
        offsets = {offset + len(surface)
                   for offset in offsets for surface, _ in candidates[:k]
                   if gold.startswith(surface, offset)}
        if not offsets:
            return False
    return len(gold) in offsets


def sentence_candidates(per_chunk: list[list[tuple[str, int]]], k: int) -> list[str]:
    """Top k distinct concatenations of cost-ordered chunk candidates."""
    if any(not candidates for candidates in per_chunk):
        return []
    initial = (0,) * len(per_chunk)
    heap = [(sum(cs[0][1] for cs in per_chunk), initial)]
    visited = {initial}
    seen = set()
    out = []
    while heap and len(out) < k:
        cost, indices = heappop(heap)
        surface = "".join(cs[i][0] for cs, i in zip(per_chunk, indices))
        if surface not in seen:
            seen.add(surface)
            out.append(surface)
        for j, (cs, i) in enumerate(zip(per_chunk, indices)):
            if i + 1 == len(cs):
                continue
            nxt = indices[:j] + (i + 1,) + indices[j + 1:]
            if nxt not in visited:
                visited.add(nxt)
                heappush(heap, (cost - cs[i][1] + cs[i + 1][1], nxt))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--k", type=int, nargs="+", default=[1, 3, 8, 30])
    parser.add_argument("--no-user-lexicon", action="store_true")
    parser.add_argument("--summary", type=Path, help="write counts and evaluation conditions as JSON")
    parser.add_argument("--details", type=Path, help="write per-item candidates as JSONL (contains text)")
    args = parser.parse_args()
    if not args.k or min(args.k) < 1:
        parser.error("k must be positive")
    if not args.data.exists():
        parser.error("AJIMEE data missing; run python3 -m tools.wp0.fetch_ajimee")
    raw = args.data.read_bytes()
    items = json.loads(raw)
    if not isinstance(items, list) or not items:
        parser.error("AJIMEE data must be a nonempty JSON array")
    ks = sorted(set(args.k))
    lex = load_lex(user_lexicon=not args.no_user_lexicon)
    reverse = reverse_table(load_table())
    counts = Counter()
    times, details = [], []
    for item in items:
        reading = hiragana(item["input"])
        golds = item["expected_output"]
        if not isinstance(golds, list) or not golds or not all(isinstance(g, str) for g in golds):
            raise ValueError("expected_output must be a nonempty list of strings")
        per_chunk, anchored, best = [], [], []
        elapsed = 0.0
        left_id = BOS_ID
        for is_kana, text in chunks(reading, reverse):
            if not is_kana:
                per_chunk.append([(text, 0)])
                anchored.append([(text, 0)])
                best.append(text)
                left_id = left_id_after(text, lex)
                continue
            tokens = [reverse[c] for c in text]
            decoded, _ = decode_tokens(tokens, lex, left_id=left_id)
            best.append(decoded.surface)
            start = time.perf_counter()
            results, _ = nbest_tokens(tokens, lex, k=max(ks), left_id=left_id)
            elapsed += (time.perf_counter() - start) * 1000
            cs = [(d.surface, d.score) for d in results]
            if [score for _, score in cs] != sorted(score for _, score in cs):
                raise ValueError("n-best candidates are not cost ordered")
            per_chunk.append(cs)
            anchored.append([(decoded.surface, decoded.score)]
                            + [c for c in cs if c[0] != decoded.surface])
        counts["n"] += 1
        counts["decode_1best"] += "".join(best) in golds
        sentences = sentence_candidates(per_chunk, max(ks))
        for k in ks:
            counts[f"sentence@{k}"] += any(g in sentences[:k] for g in golds)
            counts[f"chunk_oracle@{k}"] += any(chunk_reaches(per_chunk, g, k) for g in golds)
            counts[f"with_1best_chunk_oracle@{k}"] += any(
                chunk_reaches(anchored, g, k) for g in golds)
        times.append(elapsed)
        if args.details:
            details.append(dict(reading=reading, gold=golds, decode_1best="".join(best),
                                chunks=per_chunk, with_1best_chunks=anchored,
                                sentences=sentences, ms=round(elapsed, 3)))
    times.sort()
    summary = dict(data=str(args.data), sha256=hashlib.sha256(raw).hexdigest(),
                   mode="amb", user_lexicon=not args.no_user_lexicon, learning=False,
                   context=False, prepend_1best=False, k=ks, counts=dict(counts),
                   nbest_ms=dict(mean=statistics.mean(times),
                                 p95=times[min(len(times) - 1, int(len(times) * .95))],
                                 max=max(times)))
    print(f"AJIMEE amb: n={counts['n']}; sha256={summary['sha256']}")
    print(f"User lexicon: {'off' if args.no_user_lexicon else 'on'}; learning/context: off")
    print("Raw nbest only (no 1-best prepend); symbols pass through")
    print("chunk_oracle@k = per-chunk top-k product (Fable E5); sentence@k = whole-sentence top-k")
    print("with_1best_chunk_oracle@k additionally prepends decode_tokens 1-best (Fable E4)")
    for metric, count in counts.items():
        if metric != "n":
            print(f"{metric}: {count / counts['n'] * 100:.1f}% ({count}/{counts['n']})")
    ms = summary["nbest_ms"]
    print(f"nbest ms/sentence mean/p95/max: {ms['mean']:.3f}/{ms['p95']:.3f}/{ms['max']:.3f}")
    if args.summary:
        args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.details:
        args.details.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in details),
                                encoding="utf-8")


if __name__ == "__main__":
    main()
