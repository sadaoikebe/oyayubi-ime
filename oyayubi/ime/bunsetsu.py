"""Segment boundaries and exact, lazy candidate search, independent of 1-best pruning."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from heapq import heappop, heappush
from itertools import count
from typing import Iterator

from tools.wp0.decode import Piece
from tools.wp0.mozc_dict import BOS_ID, DICT_DIR, UNK_COST, MozcLex
from tools.wp0.token_viterbi import BOUNDARY_RID, _end_cost, _spans_from, _word_cost

# Half a single unknown-character penalty. This bounds implausible dictionary
# analyses, not the number of homonyms, and never limits the kana escape route.
COST_WINDOW = UNK_COST // 2
PAGE_SIZE = 9


@lru_cache(maxsize=1)
def _pos_names() -> dict[int, str]:
    return {int(i): name.strip() for line in (DICT_DIR / "id.def").read_text(
        encoding="utf-8").splitlines() for i, name in [line.split(None, 1)]}


def content(lid: int) -> bool:
    p = _pos_names().get(lid, "").split(",")
    if p[0] in ("名詞", "動詞", "形容詞"):
        return p[1] not in ("接尾", "非自立")
    return p[0] in ("副詞", "連体詞", "接続詞", "感動詞", "接頭詞")


@dataclass(frozen=True)
class Candidate:
    surface: str
    reading: str
    score: int
    lid: int
    rid: int
    kind: str = "dictionary"


@dataclass
class Segment:
    start: int
    end: int
    selected: Candidate
    literal: bool = False
    fixed: bool = False
    choices: CandidateStream | None = None
    context: tuple[int, int] | None = None
    index: int = 0


def segments_from(pieces: list[Piece], offset: int) -> list[Segment]:
    # Literal pieces from ambiguous faces get their own selectable interval.
    # Stage 2A's fixed literals are instead created by ImeSession._refresh().
    groups: list[list[Piece]] = []
    for piece in pieces:
        if not groups or piece.literal or groups[-1][-1].literal or content(int(piece.pos)):
            groups.append([])
        groups[-1].append(piece)
    return [Segment(offset + g[0].start, offset + g[-1].end,
                    Candidate("".join(p.surface for p in g), "".join(p.reading for p in g),
                              0, int(g[0].pos), g[-1].rid,
                              "literal" if g[0].literal else "dictionary")) for g in groups]


def _paths(tokens: list[dict], lex: MozcLex, left: int, right: int,
           *, kana: bool) -> Iterator[Candidate]:
    """A* on the full word DAG with an exact reverse-DP potential.

    Dictionary mode keeps all paths within the score band, then deduplicates
    surfaces. Kana mode includes unknown arcs and keeps the cheapest prefix for
    each (position, RID, reading): spelling/POS variants cannot multiply work for
    the same reading. Both permit negative word/connection costs.
    """
    n = len(tokens)
    arcs = [[w for w in _spans_from(tokens, i, lex) if kana or not w.unknown]
            for i in range(n)]
    # Suffix costs for every reachable previous RID; compute iteratively so a
    # long unknown segment cannot exceed Python's recursion limit.
    rids = [set() for _ in range(n + 1)]
    initial = BOUNDARY_RID if left == BOS_ID else left
    rids[0].add(initial)
    for i in range(n):
        if rids[i]:
            for w in arcs[i]:
                rids[w.end].add(w.rid)
    suffix = [{} for _ in range(n + 1)]
    suffix[n] = {rid: _end_cost(lex, rid, right) for rid in rids[n]}
    inf = float("inf")
    # The dictionary score band must be anchored to a path with a real word.
    # A zero-cost literal alone must not displace the kana side's dictionary
    # candidates. This second potential requires at least one nonliteral arc.
    word_suffix = [{} for _ in range(n + 1)]
    word_suffix[n] = {rid: inf for rid in rids[n]}
    for i in range(n - 1, -1, -1):
        for rid in rids[i]:
            suffix[i][rid] = min((_word_cost(lex, rid, w)
                                 + suffix[w.end][w.rid] for w in arcs[i]), default=inf)
            if not kana:
                word_suffix[i][rid] = min((
                    _word_cost(lex, rid, w)
                    + (word_suffix if w.literal else suffix)[w.end][w.rid]
                    for w in arcs[i]), default=inf)
    best = suffix[0].get(initial, inf)
    if best == inf:
        return
    limit = inf if kana else word_suffix[0].get(initial, inf) + COST_WINDOW
    serial = count()
    # priority, tie break, cost-so-far, position, RID, first LID, surface, reading,
    # whether every arc so far is literal
    heap = [(best, next(serial), 0, 0, initial, BOS_ID, "", "", True)]
    seen = set()
    prefixes = {}
    while heap:
        bound, _, cost, pos, rid, lid, surface, reading, all_literal = heappop(heap)
        if bound > limit:
            break
        if kana and cost != prefixes.get((pos, rid, reading), cost):
            continue
        if pos == n:
            key = reading if kana else surface
            if key not in seen:
                seen.add(key)
                yield Candidate(surface, reading, int(bound), lid,
                                BOS_ID if rid == BOUNDARY_RID else rid,
                                "literal" if all_literal else "dictionary")
            continue
        for w in arcs[pos]:
            e = w.entry
            nc = cost + _word_cost(lex, rid, w)
            bound = nc + suffix[w.end][w.rid]
            if bound > limit:
                continue
            nr = reading + e.reading
            if kana:
                key = (w.end, w.rid, nr)
                if nc >= prefixes.get(key, inf):
                    continue
                prefixes[key] = nc
            heappush(heap, (bound, next(serial), nc, w.end, w.rid,
                            e.lid if pos == 0 else lid, surface + e.surface, nr,
                            all_literal and w.literal))


def candidates(tokens: list[dict], lex: MozcLex, left: int, right: int) -> Iterator[Candidate]:
    literals = set()
    for c in _paths(tokens, lex, left, right, kana=False):
        if c.kind == "literal":
            literals.add(c.surface)
        yield c
    # Even a reading absent from every dictionary prefix remains reachable.
    for c in _paths(tokens, lex, left, right, kana=True):
        if c.kind == "literal":
            if c.surface not in literals:
                yield c  # Literal faces also remain reachable outside the cost window.
            continue  # A literal has no kana variants.
        hira = c.reading
        kata = "".join(chr(ord(ch) + 0x60) if "ぁ" <= ch <= "ゖ" else ch for ch in hira)
        yield Candidate(hira, hira, c.score, c.lid, c.rid, "kana")
        yield Candidate(kata, hira, c.score, c.lid, c.rid, "kana")


@dataclass
class CandidateStream:
    source: Iterator[Candidate]
    cache: list[Candidate] = field(default_factory=list)
    exhausted: bool = False

    def ensure(self, index: int) -> bool:
        while len(self.cache) <= index and not self.exhausted:
            c = next(self.source, None)
            if c is None:
                self.exhausted = True
            else:
                self.cache.append(c)
        return index < len(self.cache)

    def last(self) -> int:
        while not self.exhausted:
            self.ensure(len(self.cache) + PAGE_SIZE)
        return len(self.cache) - 1

    def find(self, selected: Candidate) -> int:
        i = 0
        while self.ensure(i):
            if self.cache[i].surface == selected.surface and (
                    selected.kind not in ("kana", "literal")
                    or self.cache[i].kind == selected.kind):
                return i
            i += 1
        # A fixed spelling can fall outside the band after a neighbor changes.
        # Keep its display, with no highlighted row, until explicitly selected.
        return -1
