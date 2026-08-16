"""Joint L/R + segmentation Viterbi on the token DAG, Mozc costs.

Does not expand 2^k full readings. From each position, walks only
prefixes that exist in the dictionary, then Viterbi over word spans.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .decode import DecodeResult, Piece
from .expand import token_branches
from .mozc_dict import BOS_ID, MAX_WORD_MORA, POS_KEEP, UNK_COST, UNK_LID, Entry, MozcLex


@dataclass(slots=True)
class _Word:
    start: int
    end: int
    entry: Entry


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
            nxt = acc + kana
            if nxt in lex.prefixes:
                stack.append((pos + 1, nxt))
    have_one = {w.entry.reading for w in out if w.end == i + 1}
    for kana in token_branches(tokens[i]):
        if kana not in have_one:
            out.append(_Word(i, i + 1, Entry(kana, kana, UNK_LID, UNK_LID, UNK_COST)))
    return out


def _prune(sts: list[_St]) -> list[_St]:
    """Keep cheapest path per reading prefix.

    Unique-by-rid would collapse 既読 and 記憶 (both nouns) into one
    survivor and throw away the L/R alternative before the sentence ends.
    """
    sts.sort(key=lambda s: s.score)
    best: dict[str, _St] = {}
    for s in sts:
        if s.reading not in best:
            best[s.reading] = s
        if len(best) >= POS_KEEP:
            break
    return list(best.values())


def decode_tokens(tokens: list[dict], lex: MozcLex) -> tuple[DecodeResult, dict]:
    n = len(tokens)
    t0 = time.perf_counter()
    states: list[list[_St]] = [[] for _ in range(n + 1)]
    states[0] = [_St(0, BOS_ID, -1, -1, "")]
    words: list[_Word] = []
    n_spans = 0

    for i in range(n):
        if not states[i]:
            continue
        if len(states[i]) > POS_KEEP:
            states[i] = _prune(states[i])
        spans = _spans_from(tokens, i, lex)
        n_spans += len(spans)
        for w in spans:
            wi = len(words)
            words.append(w)
            for si, st in enumerate(states[i]):
                nsc = st.score + lex.transition(st.rid, w.entry.lid) + w.entry.cost
                states[w.end].append(
                    _St(nsc, w.entry.rid, wi, si, st.reading + w.entry.reading)
                )

    if not states[n]:
        raise RuntimeError("no path")
    states[n] = _prune(states[n])

    best_i = 0
    best_score = 10**18
    for i, st in enumerate(states[n]):
        sc = st.score + lex.transition(st.rid, BOS_ID)
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
        Piece(w.entry.surface, w.entry.reading, str(w.entry.lid), w.entry.cost)
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
        Piece(w.entry.surface, w.entry.reading, str(w.entry.lid), w.entry.cost)
        for w in path
    ]
    score = states[n][0].score  # overwritten by caller
    return DecodeResult(reading=reading, surface=surface, score=score, pieces=pieces)


def nbest_tokens(
    tokens: list[dict],
    lex: MozcLex,
    k: int = 8,
) -> tuple[list[DecodeResult], dict]:
    """Same search as decode_tokens, then unique readings at the end."""
    n = len(tokens)
    t0 = time.perf_counter()
    states: list[list[_St]] = [[] for _ in range(n + 1)]
    states[0] = [_St(0, BOS_ID, -1, -1, "")]
    words: list[_Word] = []

    for i in range(n):
        if not states[i]:
            continue
        if len(states[i]) > POS_KEEP:
            states[i] = _prune(states[i])
        for w in _spans_from(tokens, i, lex):
            wi = len(words)
            words.append(w)
            for si, st in enumerate(states[i]):
                nsc = st.score + lex.transition(st.rid, w.entry.lid) + w.entry.cost
                states[w.end].append(
                    _St(nsc, w.entry.rid, wi, si, st.reading + w.entry.reading)
                )

    if not states[n]:
        raise RuntimeError("no path")
    ranked: list[tuple[int, int, str]] = []
    for i, st in enumerate(states[n]):
        sc = st.score + lex.transition(st.rid, BOS_ID)
        ranked.append((sc, i, st.reading))
    ranked.sort(key=lambda t: t[0])

    seen: set[str] = set()
    out: list[DecodeResult] = []
    for sc, i, reading in ranked:
        if reading in seen:
            continue
        seen.add(reading)
        dec = _rebuild(words, states, i, n)
        dec.score = sc
        out.append(dec)
        if len(out) >= k:
            break
    stats = {
        "ms": round((time.perf_counter() - t0) * 1000, 3),
        "n_tokens": n,
        "nbest": len(out),
    }
    return out, stats
