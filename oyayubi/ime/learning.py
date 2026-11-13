"""Opt-in, local confirmation history. Never imported by the evaluation loaders."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import copy
import json
import math
import os
from pathlib import Path
import time
from uuid import uuid4

from oyayubi.lattice.expand import is_literal_face, token_branches
from oyayubi.lattice.mozc_dict import DEFAULT_NOUN_ID, Entry, MozcLex
from .bunsetsu import Candidate, content

DEFAULT_LEARNING_PATH = Path(__file__).resolve().parents[2] / "data" / "learning.jsonl"
PAIR_BONUS = 2400
CONTEXT_BONUS = 3600  # additional to PAIR_BONUS
HALF_LIFE_SECONDS = 30 * 24 * 60 * 60
USER_WORD_COST = 6000


def pause_marker(path: Path) -> Path:
    """While this file exists, the history is used but nothing new is recorded (S3)."""
    path = Path(path)
    return path.with_name(path.name + ".paused")


@dataclass(frozen=True)
class Confirmation:
    reading: str
    surface: str
    left: str
    timestamp: float
    cost: int
    lid: int
    rid: int
    user_word: bool = False
    version: int = 1
    record_id: str = ""  # Legacy confirmations have no undo target.


@dataclass(frozen=True)
class Suppression:
    reading: str
    surface: str
    timestamp: float
    version: int = 1
    type: str = "suppress"


@dataclass(frozen=True)
class Tombstone:
    record_ids: list[str]
    timestamp: float
    version: int = 1
    type: str = "tombstone"


class LearningStore:
    """Missing files disable reads AND writes; deletion cannot recreate history.

    Append confirmations, pair suppressions and targeted tombstones. Replay in
    file order and retain the latest surviving occurrence of each triple.
    A conversion uses a fixed clock snapshot, including its lazy candidates.
    """

    max_bonus = PAIR_BONUS + CONTEXT_BONUS

    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.now = clock()
        self.revision = 0
        self.active = False
        self._signature = None
        self._needs_newline = False
        self._records: dict[tuple[str, str, str], Confirmation] = {}
        self._pairs: dict[tuple[str, str], list[Confirmation]] = {}
        self._prefixes: set[str] = set()
        self._entries: dict[str, list[Confirmation]] = {}
        self.sync()

    def _index(self) -> None:
        self._pairs = {}
        self._entries = {}
        self._prefixes = set()
        for row in self._records.values():
            self._pairs.setdefault((row.reading, row.surface), []).append(row)
        for (reading, _), rows in self._pairs.items():
            row = rows[-1]  # File order determines state; time only controls decay.
            self._entries.setdefault(reading, []).append(row)
            self._prefixes.update(reading[:i] for i in range(1, len(reading) + 1))

    def sync(self) -> None:
        self.now = self.clock()
        try:
            st = self.path.stat()
            signature = (st.st_ino, st.st_mtime_ns, st.st_size)
            if signature == self._signature:
                return
            raw = self.path.read_text(encoding="utf-8")
            lines = raw.splitlines()
            self._needs_newline = bool(raw) and not raw.endswith("\n")
        except FileNotFoundError:
            signature, lines = None, []
        except (OSError, UnicodeError):
            signature, lines = None, []
        self.active = signature is not None
        self._signature = signature
        events = []
        tombstoned = set()
        for line in lines:
            try:
                data = json.loads(line)
                event_type = data.pop("type", "confirm")
                if event_type == "confirm":
                    row = Confirmation(**data)
                elif event_type == "suppress":
                    row = Suppression(**data)
                elif event_type == "tombstone":
                    row = Tombstone(**data)
                else:
                    continue
                if (row.version != 1
                        or not isinstance(row.timestamp, (int, float))
                        or not math.isfinite(row.timestamp)):
                    continue
                if isinstance(row, Tombstone):
                    if (not isinstance(row.record_ids, list)
                            or any(not isinstance(v, str) or not v for v in row.record_ids)):
                        continue
                    tombstoned.update(row.record_ids)
                    continue
                if (not isinstance(row.reading, str)
                        or not row.reading or is_literal_face(row.reading)
                        or not isinstance(row.surface, str) or not row.surface):
                    continue
                if isinstance(row, Confirmation) and (
                        not isinstance(row.left, str) or not isinstance(row.record_id, str)
                        or any(type(v) is not int for v in (row.cost, row.lid, row.rid))
                        or row.lid < 0 or row.rid < 0):
                    continue
            except (ValueError, TypeError, AttributeError):
                continue  # An incomplete last write must not prevent conversion.
            events.append(row)
        self._records = {}
        for row in events:
            if isinstance(row, Suppression):
                self._records = {key: value for key, value in self._records.items()
                                 if key[:2] != (row.reading, row.surface)}
                continue
            if row.record_id and row.record_id in tombstoned:
                continue
            key = (row.reading, row.surface, row.left)
            # Replacing a triple also updates its order among pairs.
            self._records.pop(key, None)
            self._records[key] = row
        self._index()
        self.revision += 1

    def snapshot(self):
        self.sync()
        return copy(self) if self.active and self._pairs else None

    def bonus(self, reading: str, surface: str, left: str) -> int:
        rows = self._pairs.get((reading, surface), ())
        pair = contextual = 0.0
        for row in rows:
            age = max(0, self.now - row.timestamp)
            weight = 2 ** (-age / HALF_LIFE_SECONDS)
            pair = weight
            if row.left == left:
                contextual = max(contextual, weight)
        return round(PAIR_BONUS * pair + CONTEXT_BONUS * contextual)

    def has_prefix(self, reading: str) -> bool:
        return reading in self._prefixes

    def learned(self):
        """The latest confirmation of every learned pair (prediction's history)."""
        for rows in self._entries.values():
            yield from rows

    def spans(self, tokens: list[dict], start: int, lex: MozcLex):
        """Virtual whole-bunsetsu arcs; original entries and costs stay intact.

        The learned trie is separate from MAX_WORD_MORA-limited Mozc traversal,
        so long confirmed unknown words also remain available.
        """
        stack = [(start, "")]
        while stack:
            pos, reading = stack.pop()
            for row in self._entries.get(reading, ()):
                if row.lid < lex.pos_size and row.rid < lex.pos_size:
                    yield pos, Entry(row.surface, reading, row.lid, row.rid, row.cost)
            if pos == len(tokens):
                continue
            for kana in token_branches(tokens[pos]):
                if tokens[pos]["kind"] != "literal" and not is_literal_face(kana):
                    nxt = reading + kana
                    if nxt in self._prefixes:
                        stack.append((pos + 1, nxt))

    @staticmethod
    def next_context(previous: str, word, *, boundary: bool = False) -> str:
        e = word.entry
        if boundary or word.start == 0 or word.literal or word.learned or content(e.lid):
            return e.surface
        return previous + e.surface

    def confirm(self, selections: list[tuple[Candidate, str]], lex: MozcLex) -> list[str]:
        self.sync()
        if not self.active or pause_marker(self.path).exists():
            return []
        rows = []
        for c, left in selections:
            if c.kind in ("literal", "number", "date", "alnum") or not c.reading or is_literal_face(c.reading):
                continue
            cost, lid, rid = c.word_cost, c.lid, c.rid
            user_word = any(r.user_word for r in self._pairs.get((c.reading, c.surface), ()))
            if c.kind == "kana":
                existing = [e for e in lex.by_reading.get(c.reading, ())
                            if e.surface == c.surface]
                if existing:
                    e = min(existing, key=lambda e: e.cost)
                    cost, lid, rid = e.cost, e.lid, e.rid
                else:
                    user_word = True
                    cost, lid, rid = USER_WORD_COST, DEFAULT_NOUN_ID, DEFAULT_NOUN_ID
            if cost is not None:
                rows.append(Confirmation(c.reading, c.surface, left, self.now,
                                         cost, lid, rid, user_word, record_id=uuid4().hex))
        return [r.record_id for r in rows] if self._append(rows) else []

    def suppress(self, reading: str, surface: str) -> bool:
        self.sync()
        return self._append([Suppression(reading, surface, self.now)])

    def undo(self, record_ids: list[str]) -> bool:
        self.sync()
        return self._append([Tombstone(record_ids, self.now)])

    def _append(self, rows: list[Confirmation | Suppression | Tombstone]) -> bool:
        if not self.active or not rows:
            return False
        payload = ("\n" if self._needs_newline else "") + "".join(
            json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in rows)
        try:
            # No O_CREAT: deleting the file while this process lives disables it.
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(payload)
        except FileNotFoundError:
            self.sync()
            return False
        except OSError:
            return False  # Persistence failure must not discard the user's commit.
        self.sync()
        return True
