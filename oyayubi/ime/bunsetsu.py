"""Segment boundaries and exact, lazy candidate search, independent of 1-best pruning."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from heapq import heappop, heappush
from itertools import count
from typing import Iterator

from tools.wp0.decode import Piece
from tools.wp0.mozc_dict import BOS_ID, DICT_DIR, UNK_COST, MozcLex
from tools.wp0.token_viterbi import _spans_from

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
    groups: list[list[Piece]] = []
    for piece in pieces:
        if not groups or content(int(piece.pos)):
            groups.append([])
        groups[-1].append(piece)
    return [Segment(offset + g[0].start, offset + g[-1].end,
                    Candidate("".join(p.surface for p in g), "".join(p.reading for p in g),
                              0, int(g[0].pos), g[-1].rid)) for g in groups]


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
    rids[0].add(left)
    for i in range(n):
        if rids[i]:
            for w in arcs[i]:
                rids[w.end].add(w.entry.rid)
    suffix = [{} for _ in range(n + 1)]
    suffix[n] = {rid: lex.transition(rid, right) for rid in rids[n]}
    inf = float("inf")
    for i in range(n - 1, -1, -1):
        for rid in rids[i]:
            suffix[i][rid] = min((lex.transition(rid, w.entry.lid) + w.entry.cost
                                 + suffix[w.end][w.entry.rid] for w in arcs[i]), default=inf)
    best = suffix[0].get(left, inf)
    if best == inf:
        return
    limit = inf if kana else best + COST_WINDOW
    serial = count()
    # priority, tie break, cost-so-far, position, RID, first LID, surface, reading
    heap = [(best, next(serial), 0, 0, left, BOS_ID, "", "")]
    seen = set()
    prefixes = {}
    while heap:
        bound, _, cost, pos, rid, lid, surface, reading = heappop(heap)
        if bound > limit:
            break
        if kana and cost != prefixes.get((pos, rid, reading), cost):
            continue
        if pos == n:
            key = reading if kana else surface
            if key not in seen:
                seen.add(key)
                yield Candidate(surface, reading, int(bound), lid, rid)
            continue
        for w in arcs[pos]:
            e = w.entry
            nc = cost + lex.transition(rid, e.lid) + e.cost
            bound = nc + suffix[w.end][e.rid]
            if bound > limit:
                continue
            nr = reading + e.reading
            if kana:
                key = (w.end, e.rid, nr)
                if nc >= prefixes.get(key, inf):
                    continue
                prefixes[key] = nc
            heappush(heap, (bound, next(serial), nc, w.end, e.rid,
                            e.lid if pos == 0 else lid, surface + e.surface, nr))


def candidates(tokens: list[dict], lex: MozcLex, left: int, right: int) -> Iterator[Candidate]:
    yield from _paths(tokens, lex, left, right, kana=False)
    # Even a reading absent from every dictionary prefix remains reachable.
    for c in _paths(tokens, lex, left, right, kana=True):
        hira = "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in c.reading)
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
                    selected.kind != "kana" or self.cache[i].kind == "kana"):
                return i
            i += 1
        # A fixed spelling can fall outside the band after a neighbor changes.
        # Keep its display, with no highlighted row, until explicitly selected.
        return -1
