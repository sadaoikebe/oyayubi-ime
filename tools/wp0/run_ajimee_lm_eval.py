"""Stage 5b: AJIMEE amb exact match with/without LM rescoring, plus latency.

Run (needs torch + transformers + sentencepiece; see docs/STAGE5B.md):
  python -m tools.wp0.run_ajimee_lm_eval --alpha 0 1 2 --k 8 --context both

One AJIMEE item = one conversion operation (one Space press): kana runs are
decoded left to right exactly as ImeSession._refresh(rescore=True) does, via
the same LmRescorer.rescore. Left context = the item's context_text (when
--context uses it) + what was already chosen in this item; the session uses
text committed on the connection instead. No learning. alpha=0 is the
unchanged decode_tokens 1-best (no LM call).
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
import time

from oyayubi.ime.lm_rescore import DEFAULT_MODEL, CausalLmScorer, LmRescorer

from .fetch_ajimee import DATA
from oyayubi.lattice.mozc_dict import load_lex
from .run_ajimee_eval import chunks, hiragana
from oyayubi.lattice.table import load_table, reverse_table
from oyayubi.lattice.token_viterbi import decode_tokens


def pct(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--alpha", type=float, nargs="+", default=[0.0, 1.0, 2.0])
    parser.add_argument("--k", type=int, nargs="+", default=[8])
    parser.add_argument("--context", choices=("yes", "no", "both"), default="both")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--ctx-tokens", type=int, default=48)
    parser.add_argument("--ctx-max", type=int, default=128)
    parser.add_argument("--variant", choices=("full", "no-incremental", "naive"),
                        default="full", help="latency ablation (scores are identical)")
    parser.add_argument("--limit", type=int, default=0, help="first N items only")
    parser.add_argument("--summary", type=Path, help="write results as JSON (no text)")
    parser.add_argument("--details", type=Path, help="per-item outputs as JSONL (contains text)")
    args = parser.parse_args()
    if not args.data.exists():
        parser.error("AJIMEE data missing; run python3 -m tools.wp0.fetch_ajimee")
    raw = args.data.read_bytes()
    items = json.loads(raw)
    if args.limit:
        items = items[:args.limit]
    lex = load_lex()
    reverse = reverse_table(load_table())
    t0 = time.perf_counter()
    scorer = CausalLmScorer(args.model, threads=args.threads, ctx_tokens=args.ctx_tokens,
                            ctx_max=args.ctx_max, kv_cache=args.variant != "naive",
                            incremental=args.variant != "no-incremental")
    load_s = time.perf_counter() - t0
    scorer.nll("", ["あ", "い"])  # warm-up, as the server does before "ready"
    try:
        import resource
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except ImportError:  # Windows
        rss_mb = None
    sha = hashlib.sha256(raw).hexdigest()
    print(f"AJIMEE amb: n={len(items)}; sha256={sha}")
    print(f"model={args.model} load={load_s:.1f}s rss={rss_mb and round(rss_mb)}MB "
          f"threads={args.threads} variant={args.variant} "
          f"ctx_tokens={args.ctx_tokens} ctx_max={args.ctx_max}")
    contexts = {"yes": [True], "no": [False], "both": [True, False]}[args.context]
    results, details = [], []
    for k in args.k:
        for alpha in args.alpha:
            for use_ctx in contexts if alpha else [False]:
                rescorer = LmRescorer(scorer, alpha=alpha, k=k)
                scorer.memo.clear()  # latency per configuration, no cross-config memo
                c = Counter()
                op_ms, lm_ms, nb_ms = [], [], []
                for item in items:
                    reading = hiragana(item["input"])
                    golds = item["expected_output"]
                    ctx = item.get("context_text") or ""
                    out, rd = "", ""
                    rescorer.last = {}
                    start = time.perf_counter()
                    for is_kana, text in chunks(reading, reverse):
                        if not is_kana:
                            out += text
                            rd += text
                            continue
                        tokens = [reverse[ch] for ch in text]
                        dec, _ = decode_tokens(tokens, lex)
                        if alpha:
                            dec = rescorer.rescore((ctx if use_ctx else "") + out,
                                                   dec, tokens, lex)
                        out += dec.surface
                        rd += dec.reading
                    ms = (time.perf_counter() - start) * 1000
                    ok = out in golds
                    c["n"] += 1
                    c["exact"] += ok
                    c["reading"] += rd == reading
                    if ctx:
                        c["ctx_n"] += 1
                        c["ctx_exact"] += ok
                    c["changed"] += rescorer.last.get("changed", 0) > 0
                    if alpha and rescorer.last:
                        op_ms.append(ms)
                        lm_ms.append(rescorer.last["lm_ms"])
                        nb_ms.append(rescorer.last["nbest_ms"])
                    if args.details:
                        details.append(dict(k=k, alpha=alpha, context=use_ctx, reading=reading,
                                            gold=golds, out=out, ok=ok, ms=round(ms, 3)))
                row = dict(k=k, alpha=alpha, context=use_ctx, counts=dict(c))
                if op_ms:
                    row["ms"] = {name: dict(median=statistics.median(v), p95=pct(v, .95),
                                            max=max(v), mean=statistics.mean(v))
                                 for name, v in (("op", op_ms), ("lm", lm_ms), ("nbest", nb_ms))}
                results.append(row)
                n = c["n"]
                line = (f"k={k:2d} alpha={alpha:<4} ctx={'Y' if use_ctx else 'N'}  "
                        f"exact={c['exact'] / n * 100:.1f}% ({c['exact']}/{n})  "
                        f"ctx-subset={c['ctx_exact']}/{c['ctx_n']}  "
                        f"reading={c['reading'] / n * 100:.1f}%")
                if op_ms:
                    m = row["ms"]
                    line += (f"  op ms median/p95/max={m['op']['median']:.1f}/"
                             f"{m['op']['p95']:.1f}/{m['op']['max']:.1f}"
                             f" (lm {m['lm']['median']:.1f}/{m['lm']['max']:.1f},"
                             f" nbest {m['nbest']['median']:.1f}/{m['nbest']['max']:.1f})")
                print(line, flush=True)
    print("scorer stats:", json.dumps(scorer.stats))
    if args.summary:
        args.summary.write_text(json.dumps(dict(
            data=str(args.data), sha256=sha, n=len(items), model=args.model,
            threads=args.threads, variant=args.variant, ctx_tokens=args.ctx_tokens,
            ctx_max=args.ctx_max, load_s=load_s, rss_mb=rss_mb, results=results,
            scorer_stats=scorer.stats), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.details:
        args.details.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in details),
                                encoding="utf-8")


if __name__ == "__main__":
    main()
