"""Joint L/R + segmentation Viterbi on the token DAG, Mozc costs.

Does not expand 2^k full readings. From each position, walks only
prefixes that exist in the dictionary, then Viterbi over word spans.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from .decode import DecodeResult, Piece
from .expand import is_literal_face, literal_text, token_branches
from .mozc_dict import BOS_ID, MAX_WORD_MORA, POS_KEEP, UNK_COST, UNK_LID, Entry, MozcLex

# Distinguish an empty run after a literal from a real dictionary RID=0.
# Empty runs have no BOS->EOS cost, just like Stage 2A's split lattice.
BOUNDARY_RID = -1


@dataclass(slots=True)
class _Word:
    start: int
    end: int
    entry: Entry
    unknown: bool = False
    literal: bool = False

    @property
    def rid(self) -> int:
        return BOUNDARY_RID if self.literal else self.entry.rid


def _word_cost(lex: MozcLex, rid: int, word: _Word) -> int:
    if word.literal:
        return 0 if rid == BOUNDARY_RID else lex.transition(rid, BOS_ID)
    return lex.transition(BOS_ID if rid == BOUNDARY_RID else rid, word.entry.lid) + word.entry.cost


def _end_cost(lex: MozcLex, rid: int, right: int = BOS_ID) -> int:
    if rid == BOUNDARY_RID:
        return 0 if right == BOS_ID else lex.transition(BOS_ID, right)
    return lex.transition(rid, right)


@dataclass(slots=True)
class _St:
    score: int
    rid: int
    word_i: int
    prev: int  # index in states[word.start]
    reading: str


def _spans_from(tokens: list[dict], i: int, lex: MozcLex) -> list[_Word]:
    out: list[_Word] = []
    stack = [(i, "")]
    while stack:
        pos, acc = stack.pop()
        if acc:
            for e in lex.by_reading.get(acc, ()):
                out.append(_Word(i, pos, e))
        if pos >= len(tokens) or (pos - i) >= MAX_WORD_MORA:
            continue
        for kana in token_branches(tokens[pos]):
            if tokens[pos]["kind"] == "literal" or is_literal_face(kana):
                continue  # A word may only traverse the reading side of a token.
            nxt = acc + kana
            if nxt in lex.prefixes:
                stack.append((pos + 1, nxt))
    have_one = {w.entry.reading for w in out if w.end == i + 1}
    for kana in token_branches(tokens[i]):
        if tokens[i]["kind"] == "literal" or is_literal_face(kana):
            # Explicit Stage 2A literals already contain their display text.
            text = kana if tokens[i]["kind"] == "literal" else literal_text(kana)
            out.append(_Word(i, i + 1, Entry(text, text, BOS_ID, BOS_ID, 0), literal=True))
        elif kana not in have_one:
            out.append(_Word(i, i + 1, Entry(kana, kana, UNK_LID, UNK_LID, UNK_COST), True))
    return out


def _prune(sts: list[_St], *, mode: Literal["rid", "reading"]) -> list[_St]:
    """Keep the cheapest path per mode key, capped at POS_KEEP.

    RID pruning preserves POS alternatives for 1-best accuracy (WP0 §10).
    Reading-prefix pruning preserves L/R pairs such as 既読/記憶 for
    future n-best context rescoring (WP0 §13), at a cost to 1-best accuracy.
    """
    sts.sort(key=lambda s: s.score)
    best: dict[int | str, _St] = {}
    for s in sts:
        key = s.rid if mode == "rid" else s.reading
        if key not in best:
            best[key] = s
        if len(best) >= POS_KEEP:
            break
    return list(best.values())


def decode_tokens(tokens: list[dict], lex: MozcLex) -> tuple[DecodeResult, dict]:
    """Decode 1-best using RID pruning for accuracy without rescoring (WP0 §10/13)."""
    n = len(tokens)
    t0 = time.perf_counter()
    states: list[list[_St]] = [[] for _ in range(n + 1)]
    states[0] = [_St(0, BOUNDARY_RID, -1, -1, "")]
    words: list[_Word] = []
    n_spans = 0

    for i in range(n):
        if not states[i]:
            continue
        if len(states[i]) > POS_KEEP:
            states[i] = _prune(states[i], mode="rid")
        spans = _spans_from(tokens, i, lex)
        n_spans += len(spans)
        for w in spans:
            wi = len(words)
            words.append(w)
            for si, st in enumerate(states[i]):
                nsc = st.score + _word_cost(lex, st.rid, w)
                states[w.end].append(
                    _St(nsc, w.rid, wi, si, st.reading + w.entry.reading)
                )

    if not states[n]:
        raise RuntimeError("no path")
    states[n] = _prune(states[n], mode="rid")

    best_i = 0
    best_score = 10**18
    for i, st in enumerate(states[n]):
        sc = st.score + _end_cost(lex, st.rid)
        if sc < best_score:
            best_score = sc
            best_i = i

    path: list[_Word] = []
    pos, idx = n, best_i
    while pos > 0:
        st = states[pos][idx]
        w = words[st.word_i]
        path.append(w)
        pos, idx = w.start, st.prev
    path.reverse()

    reading = "".join(w.entry.reading for w in path)
    surface = "".join(w.entry.surface for w in path)
    pieces = [
        Piece(w.entry.surface, w.entry.reading, str(w.entry.lid), w.entry.cost,
              w.start, w.end, w.entry.rid, w.literal)
        for w in path
    ]
    ms = (time.perf_counter() - t0) * 1000
    stats = {
        "ms": round(ms, 3),
        "n_tokens": n,
        "n_word_arcs": len(words),
        "n_spans_seen": n_spans,
        "best_score": best_score,
    }
    return (
        DecodeResult(reading=reading, surface=surface, score=best_score, pieces=pieces),
        stats,
    )


def _rebuild(words: list[_Word], states: list[list[_St]], idx: int, n: int) -> DecodeResult:
    path: list[_Word] = []
    pos = n
    while pos > 0:
        st = states[pos][idx]
        w = words[st.word_i]
        path.append(w)
        pos, idx = w.start, st.prev
    path.reverse()
    reading = "".join(w.entry.reading for w in path)
    surface = "".join(w.entry.surface for w in path)
    pieces = [
        Piece(w.entry.surface, w.entry.reading, str(w.entry.lid), w.entry.cost,
              w.start, w.end, w.entry.rid, w.literal)
        for w in path
    ]
    score = states[n][0].score  # overwritten by caller
    return DecodeResult(reading=reading, surface=surface, score=score, pieces=pieces)


def nbest_tokens(
    tokens: list[dict],
    lex: MozcLex,
    k: int = 8,
) -> tuple[list[DecodeResult], dict]:
    """Return distinct surfaces using reading-prefix pruning (WP0 §13).

    Preserve L/R alternatives for future context rescoring, accepting the
    1-best accuracy tradeoff documented in WP0 §10/13.
    """
    n = len(tokens)
    t0 = time.perf_counter()
    states: list[list[_St]] = [[] for _ in range(n + 1)]
    states[0] = [_St(0, BOUNDARY_RID, -1, -1, "")]
    words: list[_Word] = []

    for i in range(n):
        if not states[i]:
            continue
        if len(states[i]) > POS_KEEP:
            states[i] = _prune(states[i], mode="reading")
        for w in _spans_from(tokens, i, lex):
            wi = len(words)
            words.append(w)
            for si, st in enumerate(states[i]):
                nsc = st.score + _word_cost(lex, st.rid, w)
                states[w.end].append(
                    _St(nsc, w.rid, wi, si, st.reading + w.entry.reading)
                )

    if not states[n]:
        raise RuntimeError("no path")
    ranked: list[tuple[int, int, str]] = []
    for i, st in enumerate(states[n]):
        sc = st.score + _end_cost(lex, st.rid)
        ranked.append((sc, i, st.reading))
    ranked.sort(key=lambda t: t[0])

    seen: set[str] = set()
    out: list[DecodeResult] = []
    for sc, i, _reading in ranked:
        dec = _rebuild(words, states, i, n)
        dec.score = sc
        if dec.surface in seen:
            continue
        if any(p.cost >= UNK_COST for p in dec.pieces):
            continue
        seen.add(dec.surface)
        out.append(dec)
        if len(out) >= k:
            break
    stats = {
        "ms": round((time.perf_counter() - t0) * 1000, 3),
        "n_tokens": n,
        "nbest": len(out),
    }
    return out, stats
