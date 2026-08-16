"""Stdio JSON protocol for a future C++ TIP.

One JSON object per line:
  {"op":"down","vk":74,"t":123}
  {"op":"up","vk":74,"t":150}
  {"op":"timeout","t":200}
  {"op":"context","text":"確定済み"}
Reply: {"composition":"...","commit":"...","converted":false,"fsm":"S1"}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oyayubi.ime.session import ImeSession


def main() -> None:
    # Pipes from the TIP default to cp1252; Japanese composition must be UTF-8.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sess = ImeSession()
    sess.load()
    print(json.dumps({"ok": True, "msg": "ready"}, ensure_ascii=True), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        op = msg.get("op")
        commit = ""
        t = int(msg.get("t", 0))
        if op == "down":
            commit = sess.on_key(True, int(msg["vk"]), t) or ""
        elif op == "up":
            commit = sess.on_key(False, int(msg["vk"]), t) or ""
        elif op == "timeout":
            commit = sess.on_timeout(t) or ""
        elif op == "quit":
            break
        deadline = sess.fsm.timer_deadline
        print(
            json.dumps(
                {
                    "composition": sess.composition,
                    "commit": commit,
                    "converted": sess.converted,
                    "fsm": sess.fsm.state,
                    "n_tokens": len(sess.tokens),
                    "timer": deadline,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
