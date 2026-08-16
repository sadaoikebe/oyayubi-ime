"""Linear-reading Viterbi over a (surface, reading, pos, cost) lexicon.

This is the WP0 stand-in for Mozc ImmutableConverter on one reading.
Joint decode = run this on every expanded reading, keep the min score.
"""

from __future__ import annotations

from dataclasses import dataclass

UNK_CHAR = 8000
CONNECT = {
    # from -> to -> extra cost (0 = natural)
    ("N", "PRT"): 0,
    ("N", "AUX"): 0,
    ("N", "N"): 200,
    ("N", "V"): 150,
    ("N", "ADJ"): 150,
    ("N", "SUF"): 0,
    ("NUM", "SUF"): 0,
    ("NUM", "N"): 80,
    ("PRT", "N"): 0,
    ("PRT", "V"): 40,
    ("PRT", "ADJ"): 40,
    ("PRT", "ADV"): 80,
    ("PRT", "NUM"): 80,
    ("AUX", "PRT"): 40,
    ("AUX", "AUX"): 80,
    ("V", "AUX"): 0,
    ("V", "PRT"): 0,
    ("ADJ", "N"): 80,
    ("ADJ", "PRT"): 40,
    ("ADV", "V"): 0,
    ("ADV", "ADJ"): 40,
    ("ADV", "N"): 80,
    ("INT", "PRT"): 80,
    ("INT", "INT"): 200,
    ("SUF", "AUX"): 0,
    ("SUF", "PRT"): 40,
    ("BOS", "INT"): 0,
    ("BOS", "N"): 0,
    ("BOS", "ADV"): 0,
    ("BOS", "V"): 80,
    ("BOS", "PRT"): 400,
    ("INT", "EOS"): 0,
    ("AUX", "EOS"): 0,
    ("PRT", "EOS"): 40,
    ("N", "EOS"): 80,
    ("V", "EOS"): 80,
}
DEFAULT_CONNECT = 400


@dataclass
class Piece:
    surface: str
    reading: str
    pos: str
    cost: int


@dataclass
class DecodeResult:
    reading: str
    surface: str
    score: int
    pieces: list[Piece]


def build_dict(entries: list[dict]) -> dict[str, list[dict]]:
    d: dict[str, list[dict]] = {}
    for e in entries:
        d.setdefault(e["reading"], []).append(e)
    return d


def _connect(prev: str, cur: str) -> int:
    return CONNECT.get((prev, cur), DEFAULT_CONNECT)


def viterbi(reading: str, lex: dict[str, list[dict]]) -> DecodeResult:
    n = len(reading)
    # dp[i] = (score, prev_i, piece)
    inf = 10**12
    dp: list[tuple[int, int, Piece | None]] = [(inf, -1, None)] * (n + 1)
    dp[0] = (0, -1, Piece("", "", "BOS", 0))

    for i in range(n):
        if dp[i][0] >= inf:
            continue
        prev_pos = dp[i][2].pos if dp[i][2] else "BOS"
        # known words
        for j in range(i + 1, n + 1):
            span = reading[i:j]
            for e in lex.get(span, ()):
                piece = Piece(e["surface"], e["reading"], e["pos"], e["cost"])
                score = dp[i][0] + e["cost"] + _connect(prev_pos, e["pos"])
                if score < dp[j][0]:
                    dp[j] = (score, i, piece)
        # single-kana backoff
        ch = reading[i]
        piece = Piece(ch, ch, "UNK", UNK_CHAR)
        score = dp[i][0] + UNK_CHAR + _connect(prev_pos, "UNK")
        if score < dp[i + 1][0]:
            dp[i + 1] = (score, i, piece)

    # EOS
    best_score = dp[n][0] + _connect(dp[n][2].pos if dp[n][2] else "UNK", "EOS")

    pieces: list[Piece] = []
    i = n
    while i > 0:
        _sc, prev, piece = dp[i]
        assert piece is not None
        pieces.append(piece)
        i = prev
    pieces.reverse()
    surface = "".join(p.surface for p in pieces)
    return DecodeResult(reading=reading, surface=surface, score=best_score, pieces=pieces)


def joint_decode(
    readings: list[str],
    lex: dict[str, list[dict]],
    gold_reading: str | None = None,
) -> tuple[DecodeResult, list[DecodeResult]]:
    scored = [viterbi(r, lex) for r in readings]
    scored.sort(key=lambda r: (r.score, r.reading != (gold_reading or ""), r.reading))
    return scored[0], scored
