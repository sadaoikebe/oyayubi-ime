"""The user's own word list (D1), separate from data/user_lexicon.jsonl.

data/user_lexicon.jsonl is part of the evaluation condition (the hand-written
corrections the Mozc/AJIMEE scores are measured with), so personal words must
not go there. They live in their own JSONL file, are overlaid on the shared
lexicon only by the running IME (never by the evaluation loaders), and take
effect without a restart: every conversion checks the file's signature.

The overlay never mutates a reading's entry list in place. Each change builds
a new list and swaps it into lex.by_reading in one assignment, so a decode
running on another thread (the engine's sessions, the LM worker) sees either
the old list or the new one.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading

from yuragi.lattice.expand import is_literal_face
from yuragi.lattice.mozc_dict import Entry, MozcLex

DEFAULT_PERSONAL_PATH = Path.home() / ".yuragi-ime" / "personal_lexicon.jsonl"
# The median cost of the 71 hand-written entries in data/user_lexicon.jsonl:
# a registered word competes like those corrections do, not like an unknown word.
DEFAULT_COST = 2600
# Mozc id.def; a short list of what a user registers, by name.
POS_IDS = {
    "名詞": 1851, "サ変名詞": 1841, "形容動詞": 1931, "固有名詞": 1920,
    "人名": 1921, "名": 1922, "姓": 1923, "地名": 1924, "組織": 1929, "副詞": 12,
}


@dataclass(frozen=True)
class Word:
    reading: str
    surface: str
    pos: str = "名詞"
    cost: int = DEFAULT_COST

    @property
    def lid(self) -> int:
        return POS_IDS[self.pos]


def validate(reading: str, surface: str, pos: str) -> None:
    if not reading or is_literal_face(reading):
        raise ValueError("読みはひらがな（と ー）で入力してください")
    if not surface or surface != surface.strip():
        raise ValueError("単語を入力してください（前後に空白は入れられません）")
    if pos not in POS_IDS:
        raise ValueError(f"品詞「{pos}」は使えません。次から選んでください: {'、'.join(POS_IDS)}")


def read_words(path: Path) -> list[Word]:
    """Valid rows in file order; a later row for the same pair wins."""
    return scan_words(path)[0]


def scan_words(path: Path) -> tuple[list[Word], int]:
    """(valid rows as read_words, number of lines skipped as not a valid word)."""
    try:
        lines = Path(path).read_bytes().splitlines()
    except FileNotFoundError:
        return [], 0
    words: dict[tuple[str, str], Word] = {}
    skipped = 0
    for raw in lines:
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            skipped += 1
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            row = json.loads(line)
            word = Word(row["reading"], row["surface"], row.get("pos", "名詞"),
                        int(row.get("cost", DEFAULT_COST)))
            validate(word.reading, word.surface, word.pos)
        except (ValueError, TypeError, KeyError, AttributeError):
            skipped += 1  # A hand edit gone wrong must not stop conversion (REDESIGN §6.2).
            continue
        words.pop((word.reading, word.surface), None)
        words[(word.reading, word.surface)] = word
    return list(words.values()), skipped


def write_words(path: Path, words: list[Word]) -> None:
    """Replace the file atomically (a reader never sees half a file)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(json.dumps(
        {"reading": w.reading, "surface": w.surface, "pos": w.pos, "cost": w.cost},
        ensure_ascii=False) + "\n" for w in words), encoding="utf-8")
    os.replace(tmp, path)


def add_word(path: Path, reading: str, surface: str, pos: str = "名詞",
             cost: int = DEFAULT_COST) -> Word:
    validate(reading, surface, pos)
    word = Word(reading, surface, pos, cost)
    words = [w for w in read_words(path) if (w.reading, w.surface) != (reading, surface)]
    write_words(path, words + [word])
    return word


def remove_word(path: Path, reading: str, surface: str) -> bool:
    words = read_words(path)
    kept = [w for w in words if (w.reading, w.surface) != (reading, surface)]
    if len(kept) == len(words):
        return False
    write_words(path, kept)
    return True


class PersonalLexicon:
    """Keeps a lexicon's overlay equal to the file; shared by all sessions."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.revision = 0
        self.count = 0
        self.skipped = 0  # lines of the file that are not a valid word (shown to the user)
        self._signature: object = ()  # never equal to a stat signature or None
        self._original: dict[str, list[Entry] | None] = {}  # reading -> list before overlay
        self._lock = threading.Lock()

    def sync(self, lex: MozcLex) -> None:
        try:
            st = self.path.stat()
            signature = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            signature = None
        if signature == self._signature:
            return
        with self._lock:
            if signature == self._signature:
                return
            words, skipped = scan_words(self.path) if signature is not None else ([], 0)
            by_reading: dict[str, list[Word]] = {}
            for w in words:
                by_reading.setdefault(w.reading, []).append(w)
            for reading in set(self._original) | set(by_reading):
                if reading not in self._original:
                    self._original[reading] = lex.by_reading.get(reading)
                base = self._original[reading]
                if reading not in by_reading:
                    if base is None:
                        lex.by_reading.pop(reading, None)
                    else:
                        lex.by_reading[reading] = base
                    del self._original[reading]
                    continue
                merged = list(base or ())
                for w in by_reading[reading]:
                    # Like apply_user_lex: a known spelling keeps its POS and
                    # gets the cheaper cost; a new one gets the chosen POS.
                    # New Entry objects: the originals stay untouched for removal.
                    at = next((i for i, e in enumerate(merged) if e.surface == w.surface), None)
                    if at is None:
                        merged.append(Entry(w.surface, reading, w.lid, w.lid, w.cost))
                    else:
                        old = merged[at]
                        merged[at] = Entry(w.surface, reading, old.lid, old.rid,
                                           min(old.cost, w.cost))
                lex.by_reading[reading] = sorted(merged, key=lambda e: e.cost)
                # Prefixes only prune the search; extra ones after a removal are harmless.
                lex.prefixes.update(reading[:i] for i in range(1, len(reading) + 1))
            self.count = len(words)
            self.skipped = skipped
            self._signature = signature
            self.revision += 1
