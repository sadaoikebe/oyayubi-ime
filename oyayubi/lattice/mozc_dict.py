"""Load Mozc OSS text dictionary + connection matrix for token-lattice Viterbi."""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DICT_DIR = ROOT / "third_party" / "mozc_oss_dict"
MANIFEST = ROOT / "data" / "dictionary-manifest.json"
# v1 caches discarded homonyms after the eighth entry; v2 pickles name the old
# module path tools.wp0.mozc_dict. Each version bump rebuilds the cache once.
CACHE_FORMAT = "mozc_lex_v3"


def cache_name(manifest: Path = MANIFEST) -> str:
    """The cache file's name: the format and the hash of the manifest's file list.

    A cache is only ever read for the dictionary it was built from (REDESIGN
    §6.2): another dictionary has another name, so a stale cache cannot be used.
    """
    files = json.loads(Path(manifest).read_text(encoding="utf-8"))["files"]
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode("ascii")).hexdigest()
    return f"{CACHE_FORMAT}-{digest[:16]}.pkl"


CACHE = ROOT / "data" / "cache" / cache_name()
USER_LEX = ROOT / "data" / "user_lexicon.jsonl"


class DictionaryFormatError(ValueError):
    """A dictionary line that is not reading/lid/rid/cost/surface (REDESIGN §6.2).

    The files are checked against the manifest's hashes, so this never happens
    with an intact dictionary; when it does, loading stops instead of skipping.
    """


class CacheWriteError(OSError):
    """The rebuilt dictionary cache could not be written (disk space etc.)."""
DEFAULT_NOUN_ID = 1851  # 名詞,一般 in Mozc id.def

BOS_ID = 0
NUMBER_ID = 2044  # 名詞,数,アラビア数字
UNK_LID = 1  # その他,間投
UNK_COST = 12000
MAX_WORD_MORA = 16
POS_KEEP = 12  # surviving (rid, score) per token position


@dataclass(slots=True)
class Entry:
    surface: str
    reading: str
    lid: int
    rid: int
    cost: int


@dataclass
class MozcLex:
    by_reading: dict[str, list[Entry]]
    prefixes: set[str]
    connect: list[int]
    pos_size: int

    def transition(self, rid: int, lid: int) -> int:
        return self.connect[rid * self.pos_size + lid]


def left_id_after(literal: str, lex: MozcLex) -> int:
    """POS context for a kana run after a literal: a digit reads as a numeral.

    Any other literal (punctuation, Latin) is a sentence boundary, as before.
    Small test lexicons have no Mozc numeral POS and keep the boundary.
    """
    digit = literal and ("0" <= literal[-1] <= "9" or "０" <= literal[-1] <= "９")
    return NUMBER_ID if digit and NUMBER_ID < lex.pos_size else BOS_ID


# What load_lex parses to build the cache. A missing one must stop the build:
# skipping it would pickle an incomplete lexicon that is then trusted as is.
LOADER_FILES = ([f"dictionary{i:02d}.txt" for i in range(10)]
                + ["suffix.txt", "connection_single_column.txt"])
FETCH_HINT = "Run: python -m tools.wp0.fetch_mozc_dict"


def require_dict_files(names: list[str], directory: Path | None = None) -> None:
    """Raise FileNotFoundError naming every missing file and how to fetch it."""
    directory = DICT_DIR if directory is None else directory
    missing = [name for name in names if not (directory / name).exists()]
    if missing:
        raise FileNotFoundError(
            f"Mozc dict files missing in {directory}: {', '.join(missing)}. {FETCH_HINT}")


def _parse_dict_files() -> tuple[dict[str, list[Entry]], set[str]]:
    by_reading: dict[str, list[Entry]] = {}
    prefixes: set[str] = set()
    files = list(DICT_DIR.glob("dictionary*.txt")) + [DICT_DIR / "suffix.txt"]
    for path in files:
        with path.open(encoding="utf-8") as f:
            for number, line in enumerate(f, 1):
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                try:
                    reading, lid_s, rid_s, cost_s, surface = parts[:5]
                    lid, rid, cost = int(lid_s), int(rid_s), int(cost_s)
                except ValueError:  # fewer than five columns, or not numbers
                    raise DictionaryFormatError(f"{path.name}:{number}: not a dictionary "
                                                "line") from None
                if not reading or not surface:
                    raise DictionaryFormatError(f"{path.name}:{number}: empty reading "
                                                "or surface")
                e = Entry(surface, reading, lid, rid, cost)
                bucket = by_reading.setdefault(reading, [])
                bucket.append(e)
                for i in range(1, len(reading) + 1):
                    prefixes.add(reading[:i])
    # Candidate enumeration needs every homonym, including POS variants.
    for bucket in by_reading.values():
        bucket.sort(key=lambda e: e.cost)
    return by_reading, prefixes


def _parse_connection() -> tuple[list[int], int]:
    path = DICT_DIR / "connection_single_column.txt"
    with path.open(encoding="utf-8") as f:
        try:
            pos_size = int(f.readline().strip())
            vals = [int(line) for line in f]
        except ValueError:
            raise DictionaryFormatError(f"{path.name}: not a number") from None
    expected = pos_size * pos_size
    if len(vals) != expected:
        raise DictionaryFormatError(f"connection size {len(vals)} != {pos_size}^2")
    return vals, pos_size


def apply_user_lex(lex: MozcLex, path: Path | None = None) -> int:
    """Overlay personal entries. Does not rewrite the Mozc cache."""
    import json

    p = path or USER_LEX
    if not p.exists():
        return 0
    n = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        row = json.loads(line)
        reading = row["reading"]
        surface = row["surface"]
        cost = int(row["cost"])
        lid = int(row.get("lid", DEFAULT_NOUN_ID))
        rid = int(row.get("rid", lid))
        bucket = lex.by_reading.setdefault(reading, [])
        replaced = False
        for e in bucket:
            if e.surface == surface:
                e.cost = min(e.cost, cost)
                replaced = True
                break
        if not replaced:
            bucket.append(Entry(surface, reading, lid, rid, cost))
        bucket.sort(key=lambda e: e.cost)
        for i in range(1, len(reading) + 1):
            lex.prefixes.add(reading[:i])
        n += 1
    return n


def read_cache(cache: Path) -> MozcLex | None:
    """The cached lexicon; None when there is none or it cannot be read.

    Whatever is wrong with the file (written halfway, empty, from another
    format), the answer is the same: build it again from the text (§6.2).
    """
    try:
        data = cache.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        print(f"dictionary cache unreadable ({type(exc).__name__}); rebuilding",
              file=sys.stderr, flush=True)
        return None
    try:
        lex = pickle.loads(data)
        if not isinstance(lex, MozcLex):
            raise TypeError(type(lex).__name__)
    except Exception as exc:  # any unpickling failure: the cache is rebuilt
        print(f"dictionary cache unreadable ({type(exc).__name__}); rebuilding",
              file=sys.stderr, flush=True)
        return None
    return lex


def write_cache(cache: Path, lex: MozcLex) -> None:
    """Write beside the cache, then take its name: never a half-written cache."""
    tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(pickle.dumps(lex, protocol=5))
        os.replace(tmp, cache)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        finally:
            raise CacheWriteError(exc.errno, f"cannot write the dictionary cache {cache}: "
                                             f"{exc.strerror}") from exc


def load_lex(force: bool = False, *, user_lexicon: bool = True,
             on_rebuild=None) -> MozcLex:
    """The lexicon from the cache, or parsed from the text and cached.

    on_rebuild: called before a parse of the text (minutes on a slow PC).
    """
    lex = None if force else read_cache(CACHE)
    if lex is None:
        require_dict_files(LOADER_FILES)
        if on_rebuild is not None:
            on_rebuild()
        print("parsing Mozc OSS dictionary (first run)...", file=sys.stderr, flush=True)
        by_reading, prefixes = _parse_dict_files()
        connect, pos_size = _parse_connection()
        lex = MozcLex(by_reading, prefixes, connect, pos_size)
        write_cache(CACHE, lex)
        print(
            f"  readings={len(by_reading)} prefixes={len(prefixes)} "
            f"pos={pos_size} cache={CACHE}",
            file=sys.stderr,
        )
    n = apply_user_lex(lex) if user_lexicon else 0
    if n:
        print(f"  user lexicon: {n} entries from {USER_LEX.name}", file=sys.stderr)
    # Apply only after saving/loading the raw cache. Include column 0 (EOS),
    # so a zero-row entry pays the same penalty even at the end of input.
    penalty = int(os.environ.get("ZERO_ROW_PENALTY", "3000"))
    if penalty:
        for start in range(0, len(lex.connect), lex.pos_size):
            end = start + lex.pos_size
            if not any(lex.connect[start:end]):
                lex.connect[start:end] = [penalty] * lex.pos_size
    return lex
