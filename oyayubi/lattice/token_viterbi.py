"""Joint L/R + segmentation Viterbi on the token DAG, Mozc costs.

Does not expand 2^k full readings. From each position, walks only
prefixes that exist in the dictionary, then Viterbi over word spans.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from heapq import heappop, heappush
from itertools import count
from typing import Literal

from .decode import DecodeResult, Piece
from .expand import is_literal_face, literal_text, token_branches
from .mozc_dict import BOS_ID, MAX_WORD_MORA, POS_KEEP, UNK_COST, UNK_LID, Entry, MozcLex

# Distinguish an empty run after a literal from a real dictionary RID=0.
# Empty runs have no BOS->EOS cost, just like Stage 2A's split lattice.
BOUNDARY_RID = -1


@dataclass(slots=True)
class Word:
    start: int
    end: int
    entry: Entry
    unknown: bool = False
    literal: bool = False
    learned: bool = False

    @property
    def rid(self) -> int:
        return BOUNDARY_RID if self.literal else self.entry.rid


def word_cost(lex: MozcLex, rid: int, word: Word) -> int:
    if word.literal:
        return 0 if rid == BOUNDARY_RID else lex.transition(rid, BOS_ID)
    return lex.transition(BOS_ID if rid == BOUNDARY_RID else rid, word.entry.lid) + word.entry.cost


def end_cost(lex: MozcLex, rid: int, right: int = BOS_ID) -> int:
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
    context: str = ""  # current bunsetsu spelling, used only by the learning layer


def spans_from(tokens: list[dict], i: int, lex: MozcLex, learning=None) -> list[Word]:
    out: list[Word] = []
    stack = [(i, "")]
    while stack:
        pos, acc = stack.pop()
        if acc:
            for e in lex.by_reading.get(acc, ()):
                out.append(Word(i, pos, e))
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
            out.append(Word(i, i + 1, Entry(text, text, BOS_ID, BOS_ID, 0), literal=True))
        elif kana not in have_one:
            out.append(Word(i, i + 1, Entry(kana, kana, UNK_LID, UNK_LID, UNK_COST), True))
    if learning is not None:
        out.extend(Word(i, end, e, learned=True)
                   for end, e in learning.spans(tokens, i, lex))
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


def decode_tokens(tokens: list[dict], lex: MozcLex, *, learning=None,
                  left_surface: str = "", left_id: int = BOS_ID) -> tuple[DecodeResult, dict]:
    """Decode 1-best using RID pruning for accuracy without rescoring (WP0 §10/13).

    left_id is the POS to the left of the run (BOS_ID: a sentence/literal boundary).
    """
    n = len(tokens)
    t0 = time.perf_counter()
    states: list[list[_St]] = [[] for _ in range(n + 1)]
    states[0] = [_St(0, BOUNDARY_RID if left_id == BOS_ID else left_id, -1, -1, "",
                     left_surface)]
    words: list[Word] = []
    n_spans = 0

    for i in range(n):
        if not states[i]:
            continue
        if len(states[i]) > POS_KEEP:
            states[i] = _prune(states[i], mode="rid")
        spans = spans_from(tokens, i, lex, learning)
        n_spans += len(spans)
        for w in spans:
            wi = len(words)
            words.append(w)
            for si, st in enumerate(states[i]):
                nsc = st.score + word_cost(lex, st.rid, w)
                context = ""
                if learning is not None:
                    if w.learned:
                        nsc -= learning.bonus(w.entry.reading, w.entry.surface, st.context)
                    context = learning.next_context(st.context, w,
                                                    boundary=st.rid == BOUNDARY_RID)
                states[w.end].append(
                    _St(nsc, w.rid, wi, si, st.reading + w.entry.reading, context)
                )

    if not states[n]:
        raise RuntimeError("no path")
    states[n] = _prune(states[n], mode="rid")

    best_i = 0
    best_score = 10**18
    for i, st in enumerate(states[n]):
        sc = st.score + end_cost(lex, st.rid)
        if sc < best_score:
            best_score = sc
            best_i = i

    path: list[Word] = []
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
        Piece(w.entry.surface, w.entry.reading, w.entry.lid, w.entry.cost,
              w.start, w.end, w.entry.rid, w.literal, w.learned)
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


def nbest_tokens(
    tokens: list[dict],
    lex: MozcLex,
    k: int = 8,
    *,
    left_id: int = BOS_ID,
) -> tuple[list[DecodeResult], dict]:
    """Return the cheapest k distinct surfaces, in nondecreasing cost order.

    Exact forward Viterbi supplies the potential for backward A*. All eligible
    word arcs and RID states survive that pass; there is no beam, score window
    or pop cap.
    As before, unknown/high-cost (>= UNK_COST) pieces are ineligible and
    learning is not consulted. Literal arcs reset both sides to BOS/EOS.
    Each surface retains its cheapest path's reading and pieces; ties are
    deterministic but have no linguistic preference.
    """
    n = len(tokens)
    t0 = time.perf_counter()
    out: list[DecodeResult] = []
    pops = 0

    def stats() -> dict:
        return {"ms": round((time.perf_counter() - t0) * 1000, 3),
                "n_tokens": n, "nbest": len(out), "n_pops": pops}

    if k <= 0:
        return out, stats()

    def connection(rid: int, lid: int) -> int:
        # A right-hand literal (or EOS) closes the current kana run. Using
        # BOUNDARY_RID, rather than LID=0, preserves real dictionary POS=0.
        if lid == BOUNDARY_RID:
            return end_cost(lex, rid)
        return lex.transition(BOS_ID if rid == BOUNDARY_RID else rid, lid)

    incoming: list[list[Word]] = [[] for _ in range(n + 1)]
    forward: list[dict[int, int]] = [{} for _ in range(n + 1)]
    forward[0][BOUNDARY_RID if left_id == BOS_ID else left_id] = 0
    # prefix[i][lid] includes the connection into the next word, but not its
    # word cost. It is an exact completion cost for a backward suffix.
    prefix: list[dict[int, int]] = [{} for _ in range(n + 1)]
    for i in range(n):
        if not forward[i]:
            continue
        for w in spans_from(tokens, i, lex):
            if w.unknown or w.entry.cost >= UNK_COST:
                continue  # Apply the existing output policy before searching.
            lid = BOUNDARY_RID if w.literal else w.entry.lid
            if lid not in prefix[i]:
                prefix[i][lid] = min(cost + connection(rid, lid)
                                     for rid, cost in forward[i].items())
            score = prefix[i][lid] + w.entry.cost
            target = forward[w.end]
            if w.rid not in target or score < target[w.rid]:
                target[w.rid] = score
            incoming[w.end].append(w)
    if not forward[n]:
        return out, stats()  # No dictionary/literal-only covering path.

    best = min(cost + end_cost(lex, rid) for rid, cost in forward[n].items())
    serial = count()
    # bound, tie-breaker, suffix cost, start, first LID, surface, linked path
    heap = [(best, next(serial), 0, n, BOUNDARY_RID, "", None)]
    # Equal (start, first LID, surface) suffixes admit exactly the same
    # prefixes. Only their cheapest path matters for distinct-surface k-best.
    suffix_cost = {(n, BOUNDARY_RID, ""): 0}
    seen = set()
    while heap and len(out) < k:
        bound, _, cost, pos, next_lid, surface, path = heappop(heap)
        pops += 1
        if cost != suffix_cost[(pos, next_lid, surface)]:
            continue
        if pos == 0:
            if surface in seen:
                continue
            seen.add(surface)
            pieces = []
            reading = []
            while path is not None:
                w, path = path
                e = w.entry
                reading.append(e.reading)
                pieces.append(Piece(e.surface, e.reading, e.lid, e.cost,
                                    w.start, w.end, e.rid, w.literal, w.learned))
            out.append(DecodeResult("".join(reading), surface, bound, pieces))
            continue
        for w in incoming[pos]:
            e = w.entry
            lid = BOUNDARY_RID if w.literal else e.lid
            nc = cost + connection(w.rid, next_lid) + e.cost
            ns = e.surface + surface
            key = (w.start, lid, ns)
            if key in suffix_cost and nc >= suffix_cost[key]:
                continue
            suffix_cost[key] = nc
            heappush(heap, (nc + prefix[w.start][lid], next(serial), nc,
                            w.start, lid, ns, (w, path)))
    return out, stats()
