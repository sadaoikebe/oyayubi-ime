"""Load Mozc OSS text dictionary + connection matrix for token-lattice Viterbi."""

from __future__ import annotations

import pickle
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DICT_DIR = ROOT / "third_party" / "mozc_oss_dict"
CACHE = ROOT / "data" / "cache" / "mozc_lex.pkl"
USER_LEX = ROOT / "data" / "user_lexicon.jsonl"
DEFAULT_NOUN_ID = 1851  # 名詞,一般 in Mozc id.def

BOS_ID = 0
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


def _parse_dict_files() -> tuple[dict[str, list[Entry]], set[str]]:
    by_reading: dict[str, list[Entry]] = {}
    prefixes: set[str] = set()
    files = list(DICT_DIR.glob("dictionary*.txt")) + [DICT_DIR / "suffix.txt"]
    for path in files:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                reading, lid_s, rid_s, cost_s, surface = parts[:5]
                if not reading or not surface:
                    continue
                # skip readings we cannot NICOLA-encode later; keep all kana-ish
                try:
                    lid, rid, cost = int(lid_s), int(rid_s), int(cost_s)
                except ValueError:
                    continue
                e = Entry(surface, reading, lid, rid, cost)
                bucket = by_reading.setdefault(reading, [])
                bucket.append(e)
                for i in range(1, len(reading) + 1):
                    prefixes.add(reading[:i])
    # keep cheapest few surfaces per reading to cap homonyms
    for reading, bucket in by_reading.items():
        bucket.sort(key=lambda e: e.cost)
        if len(bucket) > 8:
            del bucket[8:]
    return by_reading, prefixes


def _parse_connection() -> tuple[list[int], int]:
    path = DICT_DIR / "connection_single_column.txt"
    with path.open(encoding="utf-8") as f:
        pos_size = int(f.readline().strip())
        vals = [int(line) for line in f]
    expected = pos_size * pos_size
    if len(vals) != expected:
        raise ValueError(f"connection size {len(vals)} != {pos_size}^2")
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
        if len(bucket) > 8:
            del bucket[8:]
        for i in range(1, len(reading) + 1):
            lex.prefixes.add(reading[:i])
        n += 1
    return n


def load_lex(force: bool = False) -> MozcLex:
    if CACHE.exists() and not force:
        lex = pickle.loads(CACHE.read_bytes())
    else:
        if not (DICT_DIR / "dictionary00.txt").exists():
            raise FileNotFoundError(
                f"Mozc dict missing in {DICT_DIR}. Run: python -m tools.wp0.fetch_mozc_dict"
            )
        print("parsing Mozc OSS dictionary (first run)...", file=sys.stderr)
        by_reading, prefixes = _parse_dict_files()
        connect, pos_size = _parse_connection()
        lex = MozcLex(by_reading, prefixes, connect, pos_size)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_bytes(pickle.dumps(lex, protocol=5))
        print(
            f"  readings={len(by_reading)} prefixes={len(prefixes)} "
            f"pos={pos_size} cache={CACHE}",
            file=sys.stderr,
        )
    n = apply_user_lex(lex)
    if n:
        print(f"  user lexicon: {n} entries from {USER_LEX.name}", file=sys.stderr)
    return lex
