"""Stdio JSON protocol for a future C++ TIP.

One JSON object per line:
  {"op":"down","vk":74,"t":123}
  {"op":"up","vk":74,"t":150}
  {"op":"timeout","t":200}
  {"op":"down","vk":65,"t":220,"mods":["shift"]}
  {"op":"reset"}  # focus loss or application-terminated composition; no commit
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
    return {
        "composition": sess.composition,
        "commit": commit,
        "converted": sess.converted,
        "fsm": sess.fsm.state,
        "n_tokens": len(sess.tokens),
        "timer": sess.fsm.timer_deadline,
        "candidates": sess.candidates,
        "cand_index": sess.cand_index,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-text", action="store_true", help="log composition/commit text to stderr (private input)"
    )
    args = parser.parse_args()
    # Pipes from the TIP default to cp1252; Japanese composition must be UTF-8.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sess = ImeSession()
    sess.load()
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
        reply = dispatch(sess, msg)
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
