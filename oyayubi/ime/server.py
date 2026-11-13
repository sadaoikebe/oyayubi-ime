"""JSON protocol for the TIP (stdio by default; --resident for shared TCP).

One JSON object per line:
  {"op":"down","vk":74,"t":123}
  {"op":"up","vk":74,"t":150}
  {"op":"timeout","t":200}
  {"op":"down","vk":65,"t":220,"mods":["shift"]}
  {"op":"down","vk":39,"t":250,"mods":["shift"]}  # extend focused bunsetsu
  {"op":"down","vk":37,"t":280,"mods":["shift"]}  # shrink focused bunsetsu
  {"op":"reset"}  # focus loss or application-terminated composition; no commit
  {"op":"set_direct","direct":true}
  {"op":"toggle_direct"}
  {"op":"context","text":"確定済み"}
  {"op":"stats"}  # resident only: counts since start, no text
  {"op":"down","vk":9,"t":300}  # Tab: predictions for the input, if any
Reply: {"composition":"...","commit":"...","converted":false,"fsm":"S1","pending":false}
predicting: the candidates are predictions (Tab acted; the TIP eats that Tab).
pending: the conversion awaits the LM (resident); poll with timeout at "timer".
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
from pathlib import Path

from oyayubi.ime.session import ImeSession
from oyayubi.ime.learning import DEFAULT_LEARNING_PATH
from oyayubi.ime.personal import DEFAULT_PERSONAL_PATH, PersonalLexicon
from oyayubi.ime import config
from oyayubi.ime.lm_rescore import CONTEXT_CHARS
from oyayubi.ime.predict import DEFAULT_MODE as DEFAULT_PREDICTION, MODES as PREDICTION_MODES


def dispatch(sess: ImeSession, msg: dict) -> dict:
    """Shared protocol path for stdio, resident connections and transcript tests."""
    commit = apply_op(sess, msg)
    reply = snapshot(sess, msg.get("op"))
    reply["commit"] = commit
    return reply


def apply_op(sess: ImeSession, msg: dict) -> str:
    """Apply one request to the session; return the committed text."""
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
    elif op == "context":
        sess._lm_context = msg["text"][-CONTEXT_CHARS:]
    return commit


def snapshot(sess: ImeSession, op) -> dict:
    """The reply for the session's state; the caller fills in commit."""
    return {
        "direct": sess.direct,
        "pass_through": sess.direct and op in ("down", "up"),
        "composition": sess.composition,
        "commit": "",
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
        "predicting": sess.predicting,
        "pending": False,
    }


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resident", action="store_true", help="serve multiple TIPs over loopback TCP")
    parser.add_argument("--runtime-dir", type=Path, default=None,
                        help="private endpoint/lock directory (default ~/.oyayubi-ime)")
    parser.add_argument(
        "--log-text", action="store_true", help="log composition/commit text to stderr (private input)"
    )
    parser.add_argument("--learning-file", type=Path, default=DEFAULT_LEARNING_PATH,
                        help="local UTF-8 JSONL history; missing file disables learning")
    parser.add_argument("--no-learning", action="store_true", help="disable confirmation learning")
    parser.add_argument("--personal-file", type=Path, default=DEFAULT_PERSONAL_PATH,
                        help="the user's word list (JSONL; edit with python -m oyayubi.ime.manage word)")
    parser.add_argument("--prediction", choices=PREDICTION_MODES, default=DEFAULT_PREDICTION,
                        help="Tab on unconverted input offers continuations from history "
                             "and the dictionary (tab, the default) or does nothing (off)")
    parser.add_argument("--config", type=Path, default=None,
                        help="settings file (default <runtime dir>/config.json; "
                             "command-line options override it)")
    lm = parser.add_argument_group("Stage 5b LM rescoring (on by default for --resident)")
    lm.add_argument("--lm-rescore", action="store_true", default=None,
                    help="rescore the k-best with a causal LM on Space (needs torch+transformers)")
    lm.add_argument("--no-lm-rescore", action="store_false", dest="lm_rescore",
                    help="disable LM rescoring (the resident default is on)")
    lm.add_argument("--lm-model", default=None,
                    help="HF model id or local directory (default rinna/japanese-gpt2-small)")
    lm.add_argument("--lm-alpha", type=float, default=None, help="LM weight (default 1.0)")
    lm.add_argument("--lm-k", type=int, default=None, help="candidates per kana run (default 8)")
    lm.add_argument("--lm-threads", type=int, default=None, help="torch CPU threads")
    lm.add_argument("--lm-timing", action="store_true",
                    help="log rescoring time per conversion to stderr (no text)")
    lm.add_argument("--lm-prefetch", choices=("thumb", "typing", "off"), default="thumb",
                    help="resident: start the LM early on thumb press (default), "
                         "on every keystroke, or never")
    lm.add_argument("--lm-deadline-ms", type=float, default=400,
                    help="resident: fall back to the Viterbi 1-best after this wait")
    lm.add_argument("--lm-grace-ms", type=float, default=30,
                    help="resident: how long the converting request waits for the LM")
    lm.add_argument("--lm-prefetch-delay-ms", type=float, default=30,
                    help="resident: thumb press to prefetch start")
    return parser


def build_rescorer(args):
    """Import, construct and warm up the LM rescorer (raises without torch etc.)."""
    from oyayubi.ime.lm_rescore import (DEFAULT_ALPHA, DEFAULT_K, DEFAULT_MODEL,
                                        CausalLmScorer, LmRescorer)
    rescorer = LmRescorer(
        CausalLmScorer(args.lm_model or DEFAULT_MODEL, threads=args.lm_threads),
        alpha=DEFAULT_ALPHA if args.lm_alpha is None else args.lm_alpha,
        k=DEFAULT_K if args.lm_k is None else args.lm_k)
    rescorer.scorer.nll("", ["あ", "い"])  # warm up before "ready"
    return rescorer


def load_session(args, with_lm: bool = True) -> ImeSession:
    """Load expensive resources once, for stdio or a resident SessionFactory.

    with_lm=False leaves sess.lm unset (the resident server loads it on a worker).
    """
    sess = ImeSession(learning_path=None if args.no_learning else args.learning_file,
                      personal=PersonalLexicon(args.personal_file),
                      prediction=getattr(args, "prediction", DEFAULT_PREDICTION))
    sess.load()
    if sess.prediction != "off":
        sess._sync_personal()
        # The sorted reading index, once at start rather than on the first Tab.
        sess.predict_index.keys(sess._lex, sess.personal.revision
                                if sess.personal is not None else None)
    if with_lm and args.lm_rescore:
        # The LM is optional equipment: a machine without torch/transformers
        # (e.g. a fresh install) still gets a working IME, without rescoring.
        try:
            sess.lm = build_rescorer(args)
        except Exception as exc:
            print(f"LM rescoring unavailable ({type(exc).__name__}); continuing without it",
                  file=sys.stderr, flush=True)
            sess.lm = None
    return sess


def process_request(sess: ImeSession, msg: dict, *, log_text=False, lm_timing=False) -> dict:
    rescorer = sess.lm
    if rescorer is not None:
        rescorer.last = {}
    reply = dispatch(sess, msg)
    if lm_timing and rescorer is not None and rescorer.last:
        print(json.dumps({"lm": {k: round(v, 3) if isinstance(v, float) else v
                                 for k, v in rescorer.last.items()}}),
              file=sys.stderr, flush=True)
    if log_text:
        print(json.dumps({"composition": reply["composition"], "commit": reply["commit"]},
                         ensure_ascii=False), file=sys.stderr, flush=True)
    return reply


def main() -> None:
    args = config.parse_args(argument_parser())
    if args.lm_rescore is None:
        # Resident serving is the product path: LM rescoring paid off in the
        # 2026-11-12 measurement (53 fixed vs 1 broken on the Mozc-error set),
        # and the scorer loads once per server. The stdio host stays opt-in.
        args.lm_rescore = args.resident
    # Pipes from the TIP default to cp1252; Japanese composition must be UTF-8.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if args.resident:
        from oyayubi.ime.resident import run_resident
        run_resident(args)
        return
    sess = load_session(args)
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
        reply = process_request(sess, msg, log_text=args.log_text, lm_timing=args.lm_timing)
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
