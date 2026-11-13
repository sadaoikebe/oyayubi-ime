"""Prediction: continuations of the typed reading (ATOK 推測変換, MS-IME 予測入力).

Two sources, each usable and testable on its own:

  history_predictions()     the learning store's confirmations (LearningStore)
  dictionary_predictions()  the conversion lexicon, by reading prefix

predict() merges them: history first, then the dictionary, one row per
surface. A prediction is strictly longer than what was typed; it is never the
plain conversion of the input (Space already offers that).

What the learning store holds decides how history is used: one row per
confirmed bunsetsu (reading, surface, left context, time), kept per pair with
its latest occurrence, decayed with a 30-day half-life and boosted when the
left context matches. LearningStore.bonus() is exactly that ranking, so a
history prediction is ranked by it: recent and same-context first. Suppressed
pairs (Shift+F6) are already gone from the store, so they never come back as
predictions either.

The typed input is a token sequence, not one string: an ambiguous thumb chord
reads either face. The readings are enumerated like the learning store's own
whole-bunsetsu spans, pruned by the source's prefix set, so the walk stays
small even when many tokens are ambiguous.

Presentation (ImeSession): nothing is shown or computed while typing. Tab on
unconverted input asks for predictions of the input's end (the longest tail
that has any, starting at one of the last MAX_TAILS word boundaries of the
live conversion); they open in the ordinary
candidate window and are moved, selected and committed with the ordinary
candidate keys. With no prediction, Tab does exactly what it did before.
"""
from __future__ import annotations

from bisect import bisect_left
from heapq import nsmallest
from typing import Callable, Iterable

from oyayubi.lattice.expand import is_literal_face, token_branches
from oyayubi.lattice.mozc_dict import MozcLex
from .bunsetsu import Candidate, content

MODES = ("off", "tab")  # config "prediction"; tab: shown only when Tab asks
DEFAULT_MODE = "tab"
MAX_PREDICTIONS = 9  # one candidate page
HISTORY_MIN_KANA = 2  # a learned word comes back from its first two kana
DICTIONARY_MIN_KANA = 3  # shorter prefixes match too much of the dictionary
MAX_EXTRA_KANA = 8  # a continuation, not a whole sentence
MAX_READINGS = 64  # bound on ambiguous readings walked per source
MAX_TAILS = 3  # how many words back (1-best boundaries) a prediction may start


def typed_readings(tokens: list[dict], is_prefix: Callable[[str], bool],
                   minimum: int) -> list[str]:
    """Every reading of all tokens that is a prefix in the source (DFS, pruned)."""
    if not tokens or any(t["kind"] == "literal" for t in tokens):
        return []  # a digit or punctuation inside: not one word being typed
    out: list[str] = []
    stack = [(0, "")]
    while stack and len(out) < MAX_READINGS:
        pos, reading = stack.pop()
        if pos == len(tokens):
            if len(reading) >= minimum:
                out.append(reading)
            continue
        for kana in token_branches(tokens[pos]):
            if is_literal_face(kana):
                continue
            nxt = reading + kana
            if is_prefix(nxt):
                stack.append((pos + 1, nxt))
    return out


def history_predictions(store, tokens: list[dict], left_surface: str = "") -> list[Candidate]:
    """Learned bunsetsu whose reading continues the input, best first.

    store is a LearningStore snapshot (None: no history).
    """
    if store is None:
        return []
    readings = typed_readings(tokens, store.has_prefix, HISTORY_MIN_KANA)
    if not readings:
        return []
    rows = []
    for row in store.learned():
        if any(len(row.reading) > len(r) and row.reading.startswith(r) for r in readings):
            rows.append((-store.bonus(row.reading, row.surface, left_surface),
                         -row.timestamp, row))
    rows.sort(key=lambda item: item[:2])
    return [Candidate(row.surface, row.reading, 0, row.lid, row.rid, "predict", row.cost)
            for _, _, row in rows]


class ReadingIndex:
    """The lexicon's readings in sorted order, for prefix ranges.

    Built once (about 0.3 s for the full dictionary; the server builds it at
    start) and rebuilt when the personal word list changes (revision) or the
    reading table changes size. A changed list for an existing reading is
    read live.
    """

    def __init__(self) -> None:
        self._keys: list[str] = []
        self._stamp: tuple | None = None

    def keys(self, lex: MozcLex, revision: object = None) -> list[str]:
        stamp = (id(lex), len(lex.by_reading), revision)
        if stamp != self._stamp:
            self._keys = sorted(lex.by_reading)
            self._stamp = stamp
        return self._keys


def dictionary_predictions(lex: MozcLex, index: ReadingIndex, tokens: list[dict],
                           limit: int = MAX_PREDICTIONS,
                           revision: object = None) -> list[Candidate]:
    """Content words whose reading continues the input, cheapest first."""
    readings = typed_readings(tokens, lex.prefixes.__contains__, DICTIONARY_MIN_KANA)
    if not readings:
        return []
    keys = index.keys(lex, revision)
    found = []
    for typed in readings:
        at = bisect_left(keys, typed)
        while at < len(keys) and keys[at].startswith(typed):
            reading = keys[at]
            at += 1
            if reading == typed or len(reading) > len(typed) + MAX_EXTRA_KANA:
                continue
            for e in lex.by_reading.get(reading, ()):
                if content(e.lid):
                    found.append((e.cost, reading, e))
    out: list[Candidate] = []
    seen: set[str] = set()
    for cost, reading, e in nsmallest(limit * 4, found, key=lambda item: item[:2]):
        if e.surface not in seen:
            seen.add(e.surface)
            out.append(Candidate(e.surface, reading, 0, e.lid, e.rid, "predict", cost))
    return out[:limit]


def merge(*sources: Iterable[Candidate], limit: int = MAX_PREDICTIONS) -> list[Candidate]:
    """Sources in order of trust, one row per surface."""
    out: list[Candidate] = []
    seen: set[str] = set()
    for source in sources:
        for c in source:
            if c.surface not in seen:
                seen.add(c.surface)
                out.append(c)
                if len(out) == limit:
                    return out
    return out


def predict(tokens: list[dict], lex: MozcLex, index: ReadingIndex, store,
            left_surface: str = "", revision: object = None) -> list[Candidate]:
    """History rows first, then dictionary rows, one per surface."""
    return merge(history_predictions(store, tokens, left_surface),
                 dictionary_predictions(lex, index, tokens, revision=revision))
