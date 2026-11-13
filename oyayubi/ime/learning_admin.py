"""Looking after the learning history file (D2): list, forget, promote, the
learning setting (on/paused/off), compact, clear.

Everything here goes through LearningStore's replay, so what is listed is
exactly what conversion uses. Rewrites (compact, clear) replace the file
atomically; a running IME keeps appending to the new file, because it opens
the path for every write. Run them while you are not typing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path

from . import personal
from .learning import LearningStore, write_history


@dataclass(frozen=True)
class LearnedPair:
    reading: str
    surface: str
    contexts: int  # distinct left contexts confirmed
    last: float  # timestamp of the latest confirmation
    user_word: bool  # created from a kana spelling absent from the dictionary


def pairs(path: Path) -> list[LearnedPair]:
    """Surviving (reading, surface) pairs, most recent first."""
    store = LearningStore(path)
    out = []
    for (reading, surface), rows in store.pair_rows():
        out.append(LearnedPair(reading, surface, len(rows), max(r.timestamp for r in rows),
                               any(r.user_word for r in rows)))
    return sorted(out, key=lambda p: -p.last)


def search(path: Path, query: str) -> list[LearnedPair]:
    return [p for p in pairs(path) if query in p.reading or query in p.surface]


def forget(path: Path, reading: str, surface: str) -> bool:
    """The same suppression as F6 on the candidate; False if nothing was learned."""
    store = LearningStore(path)
    if not store.has_pair(reading, surface):
        return False
    return store.suppress(reading, surface)


def is_learned(path: Path, reading: str, surface: str) -> bool:
    return LearningStore(path).has_pair(reading, surface)


def promote(path: Path, personal_path: Path, reading: str, surface: str,
            pos: str = "名詞") -> personal.Word | None:
    """Register a learned pair as a personal word, so it survives clearing the
    history; None if the pair was never learned (register it as a word instead)."""
    if not is_learned(path, reading, surface):
        return None
    return personal.add_word(personal_path, reading, surface, pos)


def state(settings) -> str:
    """on, paused or off (the learning setting, REDESIGN §6.7)."""
    from .config import learning_mode
    return learning_mode(settings)


def set_mode(settings, mode: str) -> None:
    """Write the learning setting; the running engine reads it when it starts."""
    from .config import set_learning_mode
    from .server import argument_parser
    set_learning_mode(settings, argument_parser(), mode)


def _rewrite(path: Path, rows) -> None:
    write_history(path, rows)


def compact(path: Path) -> tuple[int, int]:
    """Keep only the surviving confirmations; return (lines before, lines after).

    Suppressions and tombstones only remove earlier confirmations, so after the
    replay they carry no information and are dropped with what they removed.
    """
    path = Path(path)
    store = LearningStore(path)
    if not store.active:
        raise FileNotFoundError(path)
    before = store.lines
    rows = store.records()
    _rewrite(path, rows)
    return before, len(rows)


def clear_history(path: Path, *, keep_user_words: bool = True) -> int:
    """Erase the history; by default keep the words learned from kana spellings.

    Those words exist nowhere else (they are not in the dictionary), so losing
    them with the history is rarely what is meant. Returns the rows kept.
    """
    store = LearningStore(path)
    if not store.active:
        raise FileNotFoundError(path)
    kept = {}
    if keep_user_words:
        for row in store.records():
            if row.user_word:
                kept[(row.reading, row.surface)] = row  # file order: the latest wins
    _rewrite(path, list(kept.values()))
    return len(kept)
