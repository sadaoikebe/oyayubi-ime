"""Check the hand-written LM conversion cases (test 9) against this IME.

tests/fixtures/lm_conversion_expected.json holds 30 sentences written for this
project. Each case's `composition` and `segments` were written first as the
correct Japanese; cases the IME (dictionary + LM rescoring) did not produce
were replaced by other sentences, not by the IME's output. The test never
regenerates the file.

Run from the repo root with torch/transformers and the model cached to list
the cases the IME does not produce:
  HF_HUB_OFFLINE=1 ../fable-experiments/.venv/bin/python \
      tests/fixtures/make_lm_conversion_fixture.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from yuragi.chord.fsm import AmbShift, Plain  # noqa: E402
from yuragi.ime.lm_rescore import CausalLmScorer, LmRescorer  # noqa: E402
from yuragi.ime.session import ImeSession, Literal  # noqa: E402
from yuragi.lattice.table import load_table, reverse_table  # noqa: E402

OUT = Path(__file__).with_name("lm_conversion_expected.json")


def session_tokens(reading: str, reverse: dict) -> list:
    out = []
    for ch in reading:
        token = reverse.get(ch)
        if token is None:
            out.append(Literal(ch))
        elif token["kind"] == "plain":
            out.append(Plain(token["key"]))
        else:
            out.append(AmbShift(token["key"]))
    return out


def convert(sess: ImeSession, case: dict, reverse: dict) -> dict:
    sess.reset()
    sess._lm_context = case["context"]
    sess.tokens = session_tokens(case["input"], reverse)
    sess._refresh()
    sess._refresh(rescore=True)
    return {"composition": sess.composition,
            "segments": [[s.start, s.end, s.selected.surface] for s in sess.segments]}


def main() -> None:
    reverse = reverse_table(load_table())
    sess = ImeSession(learning_path=None)
    sess.load()
    sess.lm = LmRescorer(CausalLmScorer())
    rows = json.loads(OUT.read_text(encoding="utf-8"))
    misses = 0
    for row in rows:
        got = convert(sess, row, reverse)
        if got != {"composition": row["composition"], "segments": row["segments"]}:
            misses += 1
            print(f"expected: {' / '.join(s[2] for s in row['segments'])}")
            print(f"     got: {' / '.join(s[2] for s in got['segments'])}")
    print(f"{len(rows) - misses} of {len(rows)} cases match")


if __name__ == "__main__":
    main()
