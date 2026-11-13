"""Local confirmation history (REDESIGN §6.7). Never imported by the evaluation loaders.

The file is an append-only JSONL log of confirmations, pair suppressions and
targeted tombstones. Its meaning is the replay in file order: for every
(reading, surface, left) triple, the latest confirmation that is neither
tombstoned nor older than the last suppression of its pair.

Costs that stay constant however long the history grows:

- What this process appends is applied to the in-memory index directly and the
  read position moves past it; the file is not read back.
- What another process appends (the management window) is read from the
  remembered position on: every conversion compares the file's size with it.
  Only a file that shrank or was replaced (compaction, clear) is read again
  from the start.
- Readers get a frozen view (snapshot) whose layers share an unchanged base
  with the live index; a change copies only the recent changes, not the index.

Compaction runs once at engine start (compact_if_needed): when the file holds
more than twice the live records and more than COMPACT_MIN_LINES lines.

Whether learning is on, paused or off is a setting (config.json "learning"),
decided by the caller: a store records only when ``recording`` is true. A store
whose file does not exist reads and writes nothing (deleting the file while the
IME runs does not bring it back); the engine creates the file at start when
learning is on.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from oyayubi.lattice.expand import is_literal_face, token_branches
from oyayubi.lattice.mozc_dict import DEFAULT_NOUN_ID, Entry, MozcLex
from .bunsetsu import Candidate, content

DEFAULT_LEARNING_PATH = Path.home() / ".oyayubi-ime" / "learning.jsonl"
PAIR_BONUS = 2400
CONTEXT_BONUS = 3600  # additional to PAIR_BONUS
HALF_LIFE_SECONDS = 30 * 24 * 60 * 60
USER_WORD_COST = 6000
COMPACT_MIN_LINES = 5000  # REDESIGN §11
LEARN_EXPIRE_DAYS = 365  # REDESIGN §11; words the user taught (user_word) never expire
READ_RETRY_SECONDS = 10  # an unreadable file at start is read once more after this
_LAYER_MERGE = 4096  # recent changes kept apart from the shared base before merging


def pause_marker(path: Path) -> Path:
    """The pre-§6.7 pause switch: a file next to the history.

    Only read as a migration: with no "learning" setting, an existing marker
    means paused (config.learning_mode).
    """
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


_EVENT_TYPES = {"confirm": Confirmation, "suppress": Suppression, "tombstone": Tombstone}
_MISSING = object()
_DELETED = object()


def parse_line(raw: bytes):
    """One history line: an event, or None when it is not JSON with the needed fields."""
    try:
        data = json.loads(raw.decode("utf-8"))
        cls = _EVENT_TYPES[data.pop("type", "confirm")]
        return cls(**data)
    except (ValueError, TypeError, KeyError, AttributeError):  # UnicodeError is a ValueError
        return None


class _Layer:
    """A read-mostly mapping: a base dict that is never changed in place, plus
    recent changes. freeze() gives readers their own copy of the changes only."""

    __slots__ = ("base", "top")

    def __init__(self, base: dict, top: dict | None = None):
        self.base = base
        self.top = {} if top is None else top

    def get(self, key, default=None):
        value = self.top.get(key, _MISSING)
        if value is _MISSING:
            return self.base.get(key, default)
        return default if value is _DELETED else value

    def __contains__(self, key) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def set(self, key, value) -> None:
        self.top[key] = value

    def delete(self, key) -> None:
        self.top[key] = _DELETED

    def items(self):
        for key, value in self.base.items():
            if key not in self.top:
                yield key, value
        for key, value in self.top.items():
            if value is not _DELETED:
                yield key, value

    def __bool__(self) -> bool:
        return any(True for _ in self.items())

    def freeze(self) -> "_Layer":
        return _Layer(self.base, dict(self.top))

    def merged(self) -> "_Layer":
        """The same content with an empty change set (a new base; the old one stays)."""
        if len(self.top) <= _LAYER_MERGE:
            return self
        base = self.base.copy()  # C-level copies: about 1 ms per 100,000 keys
        base.update(self.top)
        for key in [k for k, v in self.top.items() if v is _DELETED]:
            del base[key]
        return _Layer(base)


class LearningView:
    """What a conversion reads: the index at one moment, with that moment's clock.

    A conversion uses one view, including its lazy candidates, so another
    session's commit cannot change a candidate list while it is shown.
    """

    max_bonus = PAIR_BONUS + CONTEXT_BONUS

    def __init__(self, pairs: _Layer, entries: _Layer, prefixes: _Layer, first: _Layer,
                 now: float, revision: int):
        self._pairs = pairs  # (reading, surface) -> rows by replay order
        self._entries = entries  # reading -> latest row of each pair, pairs by first appearance
        self._prefix_counts = prefixes  # reading prefix -> readings having it
        self._first = first  # (reading, surface) -> replay position of its first row
        self.now = now
        self.revision = revision

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
        return self._prefix_counts.get(reading, 0) > 0

    def learned(self):
        """The latest confirmation of every learned pair (prediction's history),
        readings in order of their first appearance in the replay."""
        first = self._first
        readings = sorted(self._entries.items(),
                          key=lambda item: first.get((item[0], item[1][0].surface)))
        for _, rows in readings:
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
                    if self.has_prefix(nxt):
                        stack.append((pos + 1, nxt))

    @staticmethod
    def next_context(previous: str, word, *, boundary: bool = False) -> str:
        e = word.entry
        if boundary or word.start == 0 or word.literal or word.learned or content(e.lid):
            return e.surface
        return previous + e.surface


class LearningStore(LearningView):
    """The live history of one file; snapshot() gives conversions a frozen view."""

    def __init__(self, path: Path, *, clock=time.time, recording: bool = True,
                 on_problem=None, monotonic=time.monotonic):
        self.path = Path(path)
        self.clock = clock
        self.monotonic = monotonic
        self.recording = recording  # False: learning paused (history used, nothing recorded)
        self.on_problem = on_problem  # called once per kind: "learning-read", "learning-write"
        self.active = False
        self.lines = 0  # lines of the file read so far, valid or not
        self.skipped = 0  # lines that were not JSON with the needed fields
        self.revision = 0
        self.now = clock()
        self._identity = None  # (st_dev, st_ino) of the file the index was read from
        self._offset = 0  # bytes of that file in the index
        self._seen_size = -1  # file size at the last look (a partial last line waits)
        self._needs_newline = False
        self._read_failed_at: float | None = None  # first failed read, for the one retry
        self._read_given_up = False
        self._reported: set[str] = set()
        self._frozen = None
        self._clear()
        self.sync()

    # -- index ---------------------------------------------------------------
    def _clear(self) -> None:
        self._seq = 0
        self._history: dict[tuple, list[tuple[int, Confirmation]]] = {}  # triple -> rows
        self._ids: dict[str, tuple] = {}  # record_id -> triple
        self._tombstoned: set[str] = set()
        self._pair_keys: dict[tuple, set[tuple]] = {}
        self._reading_pairs: dict[str, set[tuple]] = {}
        self._pairs = _Layer({})
        self._entries = _Layer({})
        self._prefix_counts = _Layer({})
        self._first = _Layer({})
        self._dirty: set[tuple] = set()  # pairs replayed since readers last saw them
        self._frozen = None

    def _apply(self, event) -> None:
        """Replay one event into the writer's state; readers see it at _settle()."""
        seq = self._seq
        self._seq += 1
        if isinstance(event, Confirmation):
            if event.record_id and event.record_id in self._tombstoned:
                return
            key = (event.reading, event.surface, event.left)
            pair = key[:2]
            self._history.setdefault(key, []).append((seq, event))
            if event.record_id:
                self._ids[event.record_id] = key
            self._pair_keys.setdefault(pair, set()).add(key)
            self._reading_pairs.setdefault(event.reading, set()).add(pair)
            self._dirty.add(pair)
        elif isinstance(event, Suppression):
            pair = (event.reading, event.surface)
            for key in self._pair_keys.pop(pair, ()):
                for _, row in self._history.pop(key):
                    if row.record_id:
                        self._ids.pop(row.record_id, None)
            self._dirty.add(pair)
        else:
            for record_id in event.record_ids:
                self._tombstoned.add(record_id)
                key = self._ids.pop(record_id, None)
                if key is None:
                    continue
                rows = [item for item in self._history[key] if item[1].record_id != record_id]
                if rows:
                    self._history[key] = rows
                else:
                    del self._history[key]
                    self._pair_keys[key[:2]].discard(key)
                self._dirty.add(key[:2])

    def _settle(self) -> None:
        """Make the replayed events visible to readers (only the pairs they touched)."""
        if self._dirty:
            self._frozen = None
            readings = set()
            for pair in self._dirty:
                current = sorted(self._history[key][-1]
                                 for key in self._pair_keys.get(pair, ()))
                if current:
                    self._pairs.set(pair, tuple(row for _, row in current))
                    self._first.set(pair, current[0][0])
                else:
                    self._pair_keys.pop(pair, None)
                    self._pairs.delete(pair)
                    self._first.delete(pair)
                readings.add(pair[0])
            self._dirty = set()
            for reading in readings:
                self._refresh_reading(reading)
        self.revision += 1
        for name in ("_pairs", "_entries", "_prefix_counts", "_first"):
            setattr(self, name, getattr(self, name).merged())

    def _refresh_reading(self, reading: str) -> None:
        live = sorted((p for p in self._reading_pairs.get(reading, ()) if p in self._pair_keys),
                      key=self._first.get)
        had = reading in self._entries
        if live:
            self._reading_pairs[reading] = set(live)
            self._entries.set(reading, tuple(self._pairs.get(p)[-1] for p in live))
        else:
            self._reading_pairs.pop(reading, None)
            self._entries.delete(reading)
        if had != bool(live):
            step = 1 if live else -1
            for i in range(1, len(reading) + 1):
                prefix = reading[:i]
                count = self._prefix_counts.get(prefix, 0) + step
                if count:
                    self._prefix_counts.set(prefix, count)
                else:
                    self._prefix_counts.delete(prefix)

    def records(self) -> list[Confirmation]:
        """The surviving confirmations in replay order (what compaction keeps)."""
        return [row for _, row in sorted(rows[-1] for rows in self._history.values())]

    def pair_rows(self):
        """((reading, surface), rows) for every learned pair."""
        return self._pairs.items()

    def has_pair(self, reading: str, surface: str) -> bool:
        return (reading, surface) in self._pairs

    # -- reading the file ----------------------------------------------------
    def sync(self) -> None:
        """Bring the index up to the file: a stat, and a read only of what was added."""
        self.now = self.clock()
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            if self.active or self._identity is not None:
                self._clear()
                self.active = False
                self._identity = None
                self._offset = self.lines = self.skipped = 0
                self._seen_size = -1
                self.revision += 1
            return
        except OSError:
            self._read_failed()
            return
        if ((st.st_dev, st.st_ino) == self._identity and st.st_size == self._seen_size):
            return
        if self._read_failed_at is not None and not self._retry_due():
            return
        try:
            with open(self.path, "rb") as stream:
                st = os.fstat(stream.fileno())
                identity = (st.st_dev, st.st_ino)
                full = identity != self._identity or st.st_size < self._offset
                if full:
                    stream_offset = 0
                else:
                    stream_offset = self._offset
                    stream.seek(stream_offset)
                data = stream.read()
        except FileNotFoundError:
            return self.sync()  # replaced between stat and open: look again
        except OSError:
            self._read_failed()
            return
        self._read_failed_at = None
        if full:
            self._clear()
            self.lines = self.skipped = 0
            self._offset = 0
        self._identity = identity
        self._seen_size = stream_offset + len(data)
        self.active = True
        end = data.rfind(b"\n") + 1
        lines = data[:end].split(b"\n")[:-1]
        # A last line without its newline: our next append starts a new line.
        # Read from the start, it is read now (it may be complete JSON); read
        # as an addition, it waits for the rest of the line.
        self._needs_newline = end < len(data)
        if full and end < len(data):
            lines.append(data[end:])
            end = len(data)
        for raw in lines:
            self._read_line(raw)
        self._offset = stream_offset + end
        self._settle()

    def _read_line(self, raw: bytes) -> None:
        self.lines += 1
        if not raw.strip():
            return
        event = parse_line(raw)
        if event is None:
            self.skipped += 1
            return
        self._apply(event)

    def _retry_due(self) -> bool:
        if self._read_given_up:
            return False
        if self.monotonic() - self._read_failed_at < READ_RETRY_SECONDS:
            return False
        return True

    def _read_failed(self) -> None:
        """Another program may hold the file: no learning now, one more try later."""
        if self._read_failed_at is None:
            print(f"learning history unreadable; retrying once in {READ_RETRY_SECONDS} s",
                  file=sys.stderr, flush=True)
            self._read_failed_at = self.monotonic()
            return
        if not self._read_given_up and self._retry_due():
            self._read_given_up = True
            print("learning history still unreadable; learning stays off until restart",
                  file=sys.stderr, flush=True)
            self._report("learning-read")

    def _report(self, problem: str) -> None:
        if problem not in self._reported:
            self._reported.add(problem)
            if self.on_problem is not None:
                self.on_problem(problem)

    # -- what conversions read -------------------------------------------------
    def snapshot(self):
        self.sync()
        if not self.active or not self._pairs:
            return None
        if self._frozen is None:
            self._frozen = (self._pairs.freeze(), self._entries.freeze(),
                            self._prefix_counts.freeze(), self._first.freeze())
        return LearningView(*self._frozen, now=self.now, revision=self.revision)

    # -- writing -------------------------------------------------------------
    def confirm(self, selections: list[tuple[Candidate, str]], lex: MozcLex) -> list[str]:
        self.sync()
        if not self.active or not self.recording:
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
        written = self._append(rows)
        if written is None and rows:
            # Not saved (disk full, ...): this run still learns it; told once.
            for row in rows:
                self._apply(row)
            self._settle()
            self._report("learning-write")
        return [r.record_id for r in rows] if written else []

    def suppress(self, reading: str, surface: str) -> bool:
        self.sync()
        return bool(self._append([Suppression(reading, surface, self.now)]))

    def undo(self, record_ids: list[str]) -> bool:
        self.sync()
        return bool(self._append([Tombstone(record_ids, self.now)]))

    def _append(self, rows) -> bool | None:
        """True: written and indexed. False: nothing to write or no file. None: write failed."""
        if not self.active or not rows:
            return False
        payload = (("\n" if self._needs_newline else "") + "".join(
            json.dumps(asdict(r), ensure_ascii=False) + "\n" for r in rows)).encode("utf-8")
        try:
            # No O_CREAT: deleting the file while this process lives disables it.
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:
            self.sync()
            return False
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            ours = (st.st_dev, st.st_ino) == self._identity and st.st_size == self._seen_size
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
        except OSError:
            return None
        finally:
            os.close(fd)
        self._needs_newline = False
        if ours and self._offset == self._seen_size:
            # Nobody else wrote since the last look: index our own lines directly.
            for row in rows:
                self.lines += 1
                self._apply(row)
            self._offset = self._seen_size = self._offset + len(payload)
            self._settle()
        else:
            self.sync()  # someone else's lines come first: read them in file order
        return True

    # -- compaction (engine start) -------------------------------------------
    def compact_if_needed(self, *, min_lines: int = COMPACT_MIN_LINES,
                          expire_days: float = LEARN_EXPIRE_DAYS) -> tuple[int, int] | None:
        """Rewrite the file with only the live records, once, at start.

        Runs when the file has more than twice the live records and more than
        min_lines lines. Records whose last confirmation is older than
        expire_days are dropped too, except words the user taught (user_word).
        The old file becomes <name>.bak. Returns (lines before, lines after),
        or None when nothing was done.
        """
        self.sync()
        if not self.active:
            return None
        live = self.records()
        if not (self.lines > 2 * len(live) and self.lines > min_lines):
            return None
        oldest = self.clock() - expire_days * 24 * 60 * 60
        kept = [r for r in live if r.user_word or r.timestamp >= oldest]
        write_history(self.path, kept, backup=True)
        before = self.lines
        self.sync()  # a new file: read from the start
        return before, len(kept)


def write_history(path: Path, rows: list, *, backup: bool = False) -> None:
    """Replace the history file with rows; a crash leaves the old or the new file whole.

    The new file is written beside it and flushed to disk before it takes the
    name. backup=True first gives the old file a second name, <name>.bak.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as stream:
        stream.write("".join(json.dumps(asdict(r), ensure_ascii=False) + "\n"
                             for r in rows).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    if backup:
        bak = path.with_name(path.name + ".bak")
        bak.unlink(missing_ok=True)
        os.link(path, bak)
    os.replace(tmp, path)
