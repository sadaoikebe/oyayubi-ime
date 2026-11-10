"""Opt-in, local confirmation history. Never imported by the evaluation loaders."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import copy
import json
import math
import os
from pathlib import Path
import time

from tools.wp0.expand import is_literal_face, token_branches
from tools.wp0.mozc_dict import DEFAULT_NOUN_ID, Entry, MozcLex
from .bunsetsu import Candidate, content

DEFAULT_LEARNING_PATH = Path(__file__).resolve().parents[2] / "data" / "learning.jsonl"
PAIR_BONUS = 2400
CONTEXT_BONUS = 3600  # additional to PAIR_BONUS
HALF_LIFE_SECONDS = 30 * 24 * 60 * 60
USER_WORD_COST = 6000


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


class LearningStore:
    """Missing files disable reads AND writes; deletion cannot recreate history.

    Append one JSON object per confirmed bunsetsu, retain the latest occurrence
    of each triple in memory, and take maxima rather than accumulating counts.
    A conversion uses a fixed clock snapshot, including its lazy candidates.
    """

    max_bonus = PAIR_BONUS + CONTEXT_BONUS

    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.now = clock()
        self.revision = 0
        self.active = False
        self.error: str | None = None
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
            # Equal timestamps use the last confirmation in file order.
            row = max(reversed(rows), key=lambda r: r.timestamp)
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
        except (OSError, UnicodeError) as exc:
            self.error = type(exc).__name__
            signature, lines = None, []
        self.active = signature is not None
        self._signature = signature
        self._records = {}
        for line in lines:
            try:
                row = Confirmation(**json.loads(line))
                if (row.version != 1 or not isinstance(row.reading, str)
                        or not row.reading or is_literal_face(row.reading)
                        or not isinstance(row.surface, str) or not row.surface
                        or not isinstance(row.left, str)
                        or not isinstance(row.timestamp, (int, float))
                        or not math.isfinite(row.timestamp)
                        or any(type(v) is not int for v in (row.cost, row.lid, row.rid))
                        or row.lid < 0 or row.rid < 0):
                    continue
            except (ValueError, TypeError):
                continue  # An incomplete last write must not prevent conversion.
            key = (row.reading, row.surface, row.left)
            if key not in self._records or self._records[key].timestamp <= row.timestamp:
                # Replacing a triple must also update its order among pairs.
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
            pair = max(pair, weight)
            if row.left == left:
                contextual = max(contextual, weight)
        return round(PAIR_BONUS * pair + CONTEXT_BONUS * contextual)

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

    def confirm(self, selections: list[tuple[Candidate, str]], lex: MozcLex) -> None:
        self.sync()
        if not self.active:
            return
        rows = []
        for c, left in selections:
            if c.kind == "literal" or not c.reading or is_literal_face(c.reading):
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
                                         cost, lid, rid, user_word))
        if not rows:
            return
        payload = ("\n" if self._needs_newline else "") + "".join(
            json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in rows)
        try:
            # No O_CREAT: deleting the file while this process lives disables it.
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(payload)
        except FileNotFoundError:
            self.sync()
            return
        except OSError as exc:
            self.error = type(exc).__name__
            return  # Persistence failure must not discard the user's commit.
        self.sync()
