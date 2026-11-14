"""Pin the dictionary set with a SHA-256 manifest and check it (D3).

The dictionary plan (§4) starts by recording the current files as they are,
then pins upstream_commit to the google/mozc commit fetch_mozc_dict uses. This
module writes that manifest and verifies the working set against it: every
required file present with the recorded size and hash, and no unrecorded
dictionary*.txt (the loader globs them, so a stray file would change results).

  python -m yuragi.lattice.dict_manifest write --label local-snapshot-20261111
  python -m yuragi.lattice.dict_manifest verify [--deep]

--deep also parses the text files and compares them with the pickle cache,
which load_lex trusts without checking (the plan's "is the cache the text?").
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import pickle
import sys

from . import mozc_dict
from .mozc_dict import MANIFEST, ROOT, USER_LEX, _parse_connection, _parse_dict_files
FORMAT = 1
# The loader's inputs (dictionary*, suffix, connection, id.def for POS names),
# plus the evaluation set and the upstream notices that travel with them.
REQUIRED = ([f"dictionary{i:02d}.txt" for i in range(10)]
            + ["suffix.txt", "connection_single_column.txt", "id.def",
               "evaluation.tsv", "README.txt", "README.md"])


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def describe(path: Path) -> dict:
    return {"size": path.stat().st_size, "sha256": sha256(path)}


def describe_text(path: Path) -> dict:
    """describe() of the LF form: git on Windows may check the file out with CRLF."""
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def build(label: str, upstream_commit: str = "unknown") -> dict:
    return {
        "format": FORMAT,
        "label": label,
        "upstream_commit": upstream_commit,
        "dictionary_dir": str(mozc_dict.DICT_DIR.relative_to(ROOT)),
        "files": {name: describe(mozc_dict.DICT_DIR / name) for name in REQUIRED},
        # Hand-written corrections are applied separately (an evaluation
        # condition of their own), so they are recorded, not mixed in.
        "user_lexicon": {"path": str(USER_LEX.relative_to(ROOT)), **describe_text(USER_LEX)},
        # No cache hash: pickled sets follow the per-process string hash seed,
        # so a rebuilt cache never has the same bytes. --deep checks its content.
    }


def verify(manifest: dict) -> list[str]:
    """Differences between the working set and the manifest; [] if it matches."""
    problems = []
    directory = mozc_dict.DICT_DIR
    for name, want in manifest["files"].items():
        path = directory / name
        if not path.exists():
            problems.append(f"missing {name}")
        elif describe(path) != want:
            problems.append(f"changed {name}")
    recorded = set(manifest["files"])
    for path in sorted(directory.glob("dictionary*.txt")):
        if path.name not in recorded:
            problems.append(f"unrecorded {path.name} (the loader would read it)")
    if describe_text(USER_LEX) != {k: manifest["user_lexicon"][k] for k in ("size", "sha256")}:
        problems.append(f"changed {USER_LEX.name}")
    return problems


def cache_matches_text() -> list[str]:
    """Compare the pickle cache with a fresh parse of the text files."""
    cache = mozc_dict.CACHE
    if not cache.exists():
        return [f"no cache at {cache}"]
    cached = pickle.loads(cache.read_bytes())
    by_reading, prefixes = _parse_dict_files()
    connect, pos_size = _parse_connection()
    problems = []
    if cached.pos_size != pos_size or cached.connect != connect:
        problems.append("cache connection matrix differs from connection_single_column.txt")
    if cached.prefixes != prefixes:
        problems.append("cache prefixes differ from the dictionary text")
    if set(cached.by_reading) != set(by_reading):
        problems.append("cache readings differ from the dictionary text")
    else:
        key = lambda e: (e.surface, e.lid, e.rid, e.cost)  # noqa: E731
        changed = sum(sorted(map(key, cached.by_reading[r])) != sorted(map(key, b))
                      for r, b in by_reading.items())
        if changed:
            problems.append(f"cache entries differ for {changed} readings")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("write", help="record the current files")
    w.add_argument("--label", required=True)
    w.add_argument("--upstream-commit", default="unknown")
    w.add_argument("--manifest", type=Path, default=MANIFEST)
    v = sub.add_parser("verify", help="check the current files against the manifest")
    v.add_argument("--manifest", type=Path, default=MANIFEST)
    v.add_argument("--deep", action="store_true", help="also compare the cache with the text")
    v.add_argument("--dict-dir", type=Path, default=None,
                   help="check the files in this directory (the installer's shared copy)")
    args = parser.parse_args(argv)
    if getattr(args, "dict_dir", None) is not None:
        mozc_dict.DICT_DIR = args.dict_dir
    if args.command == "write":
        args.manifest.write_text(json.dumps(build(args.label, args.upstream_commit),
                                            ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        print(f"wrote {args.manifest}")
        return 0
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    problems = verify(manifest) + (cache_matches_text() if args.deep else [])
    for p in problems:
        print(p)
    print(f"{manifest['label']}: " + ("OK" if not problems else f"{len(problems)} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
