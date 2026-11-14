"""Development smoke test: the real dictionary converts as recorded.

(The installer's own check is the broker's --selftest, REDESIGN §8.2 step 3.)

Replays the project's own golden transcript (tests/transcripts/
acceptance_real_dictionary.json: real key events against the real
dictionary — the acceptance_sentence.json twin is recorded against an
inline fixture lexicon and would not match) through the session and
checks that the committed document matches the recording. This
exercises the face table, the chord FSM, the dictionary cache and the
conversion path exactly as installed — no test-only substitutes.

  python packaging/smoke.py

Exit code 0 and a PASS line when the installed tree converts correctly.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
TRANSCRIPT = ROOT / "tests" / "transcripts" / "acceptance_real_dictionary.json"


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from yuragi.ime.server import dispatch
    from yuragi.ime.session import ImeSession

    case = json.loads(TRANSCRIPT.read_text(encoding="utf-8"))
    session = ImeSession(learning_path=None)
    session.load()
    committed = ""
    for step in case["steps"]:
        reply = dispatch(session, step["event"])
        committed += reply["commit"]
    expected = case["steps"][-1]["expect"]["committed"]
    if not expected or committed != expected:
        print(f"FAIL: committed {committed!r}, expected {expected!r}")
        return 1
    print(f"PASS: smoke conversion committed {committed!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
