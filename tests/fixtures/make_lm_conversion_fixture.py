"""Pin the pre-refactor `_refresh(rescore=True)` result with the real LM (test 9).

Run once from the repo root with torch/transformers and the model cached:
  HF_HUB_OFFLINE=1 ../fable-experiments/.venv/bin/python \
      tests/fixtures/make_lm_conversion_fixture.py
Writes tests/fixtures/lm_conversion_expected.json. The test never regenerates it.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from oyayubi.chord.fsm import AmbShift, Plain  # noqa: E402
from oyayubi.ime.lm_rescore import CausalLmScorer, LmRescorer  # noqa: E402
from oyayubi.ime.session import ImeSession, Literal  # noqa: E402
from tools.wp0.fetch_ajimee import DATA  # noqa: E402
from tools.wp0.run_ajimee_eval import hiragana  # noqa: E402
from tools.wp0.run_mozc_eval import reverse_table  # noqa: E402
from tools.wp0.table import load_table  # noqa: E402

LIMIT = 30
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


def cases() -> list[dict]:
    items = json.loads(DATA.read_bytes())[:LIMIT]
    return [{"input": hiragana(item["input"]), "context": item.get("context_text") or ""}
            for item in items]


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
    rows = []
    for case in cases():
        rows.append({**case, **convert(sess, case, reverse)})
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(rows)} cases to {OUT}")


if __name__ == "__main__":
    main()
