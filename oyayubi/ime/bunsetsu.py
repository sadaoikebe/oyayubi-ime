"""Segment boundaries and exact, lazy candidate search, independent of 1-best pruning."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from heapq import heappop, heappush
from itertools import count
from typing import Iterator

from oyayubi.lattice.decode import Piece
from oyayubi.lattice.mozc_dict import BOS_ID, DICT_DIR, UNK_COST, MozcLex
from oyayubi.lattice.token_viterbi import BOUNDARY_RID, end_cost, spans_from, word_cost

from .dates import date_spellings

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
    word_cost: int | None = None  # raw internal cost, without neighbor transitions/learning


@dataclass
class Segment:
    start: int
    end: int
    selected: Candidate
    literal: bool = False
    fixed: bool = False
    choices: CandidateStream | None = None
    context: tuple | None = None
    index: int = 0


def segments_from(pieces: list[Piece], offset: int, lex: MozcLex | None = None) -> list[Segment]:
    # Literal pieces from ambiguous faces get their own selectable interval.
    # Stage 2A's fixed literals are instead created by ImeSession._refresh().
    groups: list[list[Piece]] = []
    for piece in pieces:
        if (not groups or piece.literal or piece.learned or groups[-1][-1].literal
                or content(piece.lid)):
            groups.append([])
        groups[-1].append(piece)
    return [Segment(offset + g[0].start, offset + g[-1].end,
                    Candidate("".join(p.surface for p in g), "".join(p.reading for p in g),
                              0, g[0].lid, g[-1].rid,
                              "literal" if g[0].literal else "dictionary",
                              sum(p.cost for p in g) + (sum(
                                  lex.transition(a.rid, b.lid) for a, b in zip(g, g[1:]))
                                  if lex is not None else 0))) for g in groups]


def _paths(tokens: list[dict], lex: MozcLex, left: int, right: int,
           *, kana: bool, learning=None, left_surface: str = "") -> Iterator[Candidate]:
    """A* on the full word DAG with an exact reverse-DP potential.

    Dictionary mode keeps all paths within the score band, then deduplicates
    surfaces. Kana mode includes unknown arcs and keeps the cheapest prefix for
    each (position, RID, reading): spelling/POS variants cannot multiply work for
    the same reading. Both permit negative word/connection costs.
    """
    n = len(tokens)
    arcs = [[w for w in spans_from(tokens, i, lex, learning) if kana or not w.unknown]
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
    suffix[n] = {rid: end_cost(lex, rid, right) for rid in rids[n]}
    inf = float("inf")
    # The dictionary score band must be anchored to a path with a real word.
    # A zero-cost literal alone must not displace the kana side's dictionary
    # candidates. This second potential requires at least one nonliteral arc.
    word_suffix = [{} for _ in range(n + 1)]
    word_suffix[n] = {rid: inf for rid in rids[n]}
    for i in range(n - 1, -1, -1):
        for rid in rids[i]:
            suffix[i][rid] = min((word_cost(lex, rid, w)
                                 + suffix[w.end][w.rid] for w in arcs[i]), default=inf)
            if not kana:
                word_suffix[i][rid] = min((
                    word_cost(lex, rid, w)
                    + (word_suffix if w.literal else suffix)[w.end][w.rid]
                    for w in arcs[i]), default=inf)
    best = suffix[0].get(initial, inf)
    if best == inf:
        return
    # Whole-bunsetsu bonuses apply at the terminal. Subtract their upper bound
    # from unfinished paths to keep A* admissible even with negative bonuses.
    discount = learning.max_bonus if learning is not None and not kana else 0
    limit = inf if discount or kana else word_suffix[0].get(initial, inf) + COST_WINDOW
    serial = count()
    # priority, tie break, cost-so-far, position, RID, first LID, surface, reading,
    # whether every arc so far is literal
    heap = [(best - discount, next(serial), 0, 0, initial, BOS_ID, "", "", True)]
    seen = set()
    prefixes = {}
    while heap:
        bound, _, cost, pos, rid, lid, surface, reading, all_literal = heappop(heap)
        if bound > limit:
            break
        if kana and cost != prefixes.get((pos, rid, reading), cost):
            continue
        if pos == n:
            if not kana and not all_literal and limit == inf:
                limit = bound + COST_WINDOW
            key = reading if kana else surface
            if key not in seen:
                seen.add(key)
                yield Candidate(surface, reading, int(bound), lid,
                                BOS_ID if rid == BOUNDARY_RID else rid,
                                "literal" if all_literal else "dictionary",
                                cost - lex.transition(BOS_ID if initial == BOUNDARY_RID
                                                      else initial, lid))
            continue
        for w in arcs[pos]:
            e = w.entry
            nc = cost + word_cost(lex, rid, w)
            nr = reading + e.reading
            ns = surface + e.surface
            bonus = (learning.bonus(nr, ns, left_surface)
                     if discount and w.end == n else discount)
            bound = nc + suffix[w.end][w.rid] - bonus
            if bound > limit:
                continue
            if kana:
                key = (w.end, w.rid, nr)
                if nc >= prefixes.get(key, inf):
                    continue
                prefixes[key] = nc
            heappush(heap, (bound, next(serial), nc, w.end, w.rid,
                            e.lid if pos == 0 else lid, ns, nr,
                            all_literal and w.literal))


def candidates(tokens: list[dict], lex: MozcLex, left: int, right: int,
               *, learning=None, left_surface: str = "",
               now: datetime | None = None) -> Iterator[Candidate]:
    """Dictionary rows, then kana rows, then date rows (with now)."""
    literals = set()
    dictionary = []
    for c in _paths(tokens, lex, left, right, kana=False,
                    learning=learning, left_surface=left_surface):
        if c.kind == "literal":
            literals.add(c.surface)
        dictionary.append(c)
        yield c
    # Even a reading absent from every dictionary prefix remains reachable.
    for c in _paths(tokens, lex, left, right, kana=True, learning=learning):
        if c.kind == "literal":
            if c.surface not in literals:
                yield c  # Literal faces also remain reachable outside the cost window.
            continue  # A literal has no kana variants.
        hira = c.reading
        kata = "".join(chr(ord(ch) + 0x60) if "ぁ" <= ch <= "ゖ" else ch for ch in hira)
        yield Candidate(hira, hira, c.score, c.lid, c.rid, "kana")
        yield Candidate(kata, hira, c.score, c.lid, c.rid, "kana")
    # Last, so every existing row keeps its place (and its number key).
    if now is not None:
        yield from _date_rows(dictionary, now)


def _date_rows(dictionary: list[Candidate], now: datetime) -> Iterator[Candidate]:
    """きょう → 2026/11/12 …; a kana tail (きょうは → 今日は) is kept as typed."""
    seen = {c.surface for c in dictionary}
    for c in dictionary:
        for cut in range(len(c.reading), 0, -1):
            tail = c.reading[cut:]
            spellings = date_spellings(c.reading[:cut], now)
            if spellings and c.surface.endswith(tail):
                for text in spellings:
                    if text + tail not in seen:
                        seen.add(text + tail)
                        yield Candidate(text + tail, c.reading, c.score, c.lid, c.rid, "date")
                break


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
