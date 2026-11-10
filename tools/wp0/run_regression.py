"""Run conversion regression gates: python3 -m tools.wp0.run_regression."""

from __future__ import annotations

from .mozc_dict import load_lex
from .run_mozc_eval import reverse_table
from .table import load_table
from .token_viterbi import decode_tokens, nbest_tokens


def main() -> int:
    names = (
        "おはよう 1-best",
        "どうぞ 1-best",
        "どうぞ n-best excludes 王族 and 翁",
        "きどく n-best includes 既読 and 記憶",
    )
    try:
        reverse = reverse_table(load_table())
        lex = load_lex()
    except Exception as exc:
        for name in names:
            print(f"FAIL {name}: setup error: {exc}")
        return 1

    def tokens(reading: str) -> list[dict]:
        return [reverse[kana] for kana in reading]

    def best(reading: str) -> tuple[bool, str]:
        result, _ = decode_tokens(tokens(reading), lex)
        return result.surface == reading, f"surface={result.surface!r}"

    def candidates(reading: str, expected: set[str], *, exclude: bool) -> tuple[bool, str]:
        results, _ = nbest_tokens(tokens(reading), lex, k=8)
        surfaces = {result.surface for result in results}
        matches = expected & surfaces
        passed = not matches if exclude else matches == expected
        return passed, f"surfaces={[result.surface for result in results]!r}"

    gates = (
        lambda: best("おはよう"),
        lambda: best("どうぞ"),
        lambda: candidates("どうぞ", {"王族", "翁"}, exclude=True),
        lambda: candidates("きどく", {"既読", "記憶"}, exclude=False),
    )
    all_passed = True
    for name, gate in zip(names, gates):
        try:
            passed, detail = gate()
        except Exception as exc:
            passed, detail = False, f"error: {exc}"
        print(f"{'PASS' if passed else 'FAIL'} {name}: {detail}")
        all_passed &= passed
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
