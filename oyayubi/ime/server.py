"""Stdio JSON protocol for a future C++ TIP.

One JSON object per line:
  {"op":"down","vk":74,"t":123}
  {"op":"up","vk":74,"t":150}
  {"op":"timeout","t":200}
  {"op":"down","vk":65,"t":220,"mods":["shift"]}
  {"op":"reset"}  # focus loss or application-terminated composition; no commit
  {"op":"set_direct","direct":true}
  {"op":"toggle_direct"}
  {"op":"context","text":"確定済み"}
Reply: {"composition":"...","commit":"...","converted":false,"fsm":"S1"}
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oyayubi.ime.session import ImeSession
from oyayubi.ime.learning import DEFAULT_LEARNING_PATH


def dispatch(sess: ImeSession, msg: dict) -> dict:
    """Shared protocol path for stdio and timestamped transcript tests."""
    op = msg.get("op")
    commit = ""
    t = int(msg.get("t", 0))
    if op in ("down", "up"):
        args = (op == "down", int(msg["vk"]), t)
        commit = (sess.on_key(*args, mods=msg["mods"]) if "mods" in msg
                  else sess.on_key(*args)) or ""
    elif op == "timeout":
        commit = sess.on_timeout(t) or ""
    elif op == "reset":
        sess.reset()
    elif op == "set_direct":
        sess.set_direct(msg["direct"])
    elif op == "toggle_direct":
        sess.set_direct(not sess.direct)
    return {
        "direct": sess.direct,
        "pass_through": sess.direct and op in ("down", "up"),
        "composition": sess.composition,
        "commit": commit,
        "converted": sess.converted,
        "fsm": sess.fsm.state,
        "n_tokens": len(sess.tokens),
        "timer": sess.fsm.timer_deadline,
        "candidates": sess.candidates,
        "cand_index": sess.cand_index,
        "segments": sess.segment_snapshot(),
        "focus": sess.focus,
        "candidate_page": sess.candidate_page,
        "candidate_kinds": sess.candidate_kinds,
        "has_next_page": sess.has_next_page,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-text", action="store_true", help="log composition/commit text to stderr (private input)"
    )
    parser.add_argument("--learning-file", type=Path, default=DEFAULT_LEARNING_PATH,
                        help="local UTF-8 JSONL history; missing file disables learning")
    parser.add_argument("--no-learning", action="store_true", help="disable confirmation learning")
    lm = parser.add_argument_group("Stage 5b LM rescoring (experimental, off by default)")
    lm.add_argument("--lm-rescore", action="store_true",
                    help="rescore the k-best with a causal LM on Space (needs torch+transformers)")
    lm.add_argument("--lm-model", default=None,
                    help="HF model id or local directory (default rinna/japanese-gpt2-small)")
    lm.add_argument("--lm-alpha", type=float, default=None, help="LM weight (default 1.0)")
    lm.add_argument("--lm-k", type=int, default=None, help="candidates per kana run (default 8)")
    lm.add_argument("--lm-threads", type=int, default=None, help="torch CPU threads")
    lm.add_argument("--lm-timing", action="store_true",
                    help="log rescoring time per conversion to stderr (no text)")
    args = parser.parse_args()
    # Pipes from the TIP default to cp1252; Japanese composition must be UTF-8.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sess = ImeSession(learning_path=None if args.no_learning else args.learning_file)
    sess.load()
    rescorer = None
    if args.lm_rescore:
        # Imported only behind the flag: the default server has no LM dependency.
        from oyayubi.ime.lm_rescore import (DEFAULT_ALPHA, DEFAULT_K, DEFAULT_MODEL,
                                            CausalLmScorer, LmRescorer)
        sess.lm = LmRescorer(
            CausalLmScorer(args.lm_model or DEFAULT_MODEL, threads=args.lm_threads),
            alpha=DEFAULT_ALPHA if args.lm_alpha is None else args.lm_alpha,
            k=DEFAULT_K if args.lm_k is None else args.lm_k)
        rescorer = sess.lm
        rescorer.scorer.nll("", ["あ", "い"])  # warm up before "ready"
    # The long-lived dictionary must not be traversed by full GC during typing.
    gc.freeze()
    print(json.dumps({"ok": True, "msg": "ready"}, ensure_ascii=True), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("op") == "quit":
            break
        if rescorer is not None:
            rescorer.last = {}
        reply = dispatch(sess, msg)
        if args.lm_timing and rescorer is not None and rescorer.last:
            print(json.dumps({"lm": {k: round(v, 3) if isinstance(v, float) else v
                                     for k, v in rescorer.last.items()}}),
                  file=sys.stderr, flush=True)
        if args.log_text:
            print(json.dumps({"composition": reply["composition"], "commit": reply["commit"]},
                             ensure_ascii=False), file=sys.stderr, flush=True)
        print(
            json.dumps(
                reply,
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Exception messages (e.g. invalid int input) may contain request text.
        print(f"server error: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
