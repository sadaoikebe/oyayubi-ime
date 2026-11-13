"""Download Mozc OSS dictionary text (not a full Mozc build)."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "third_party" / "mozc_oss_dict"
MANIFEST = ROOT / "data" / "dictionary-manifest.json"
# Pinned to the commit data/dictionary-manifest.json records (upstream_commit):
# all 16 manifest files match it, so a fresh fetch passes `dict verify`.
REVISION = "8be758dcc2330011d052a670b39cf8ffbaa1c253"
BASE = f"https://raw.githubusercontent.com/google/mozc/{REVISION}/src/data/dictionary_oss/"
FILES = (
    ["README.txt", "README.md", "id.def", "suffix.txt", "evaluation.tsv"]
    + [f"dictionary{i:02d}.txt" for i in range(10)]
    + ["connection_single_column.txt"]
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _expected_hashes() -> dict[str, str]:
    """name -> SHA-256 from the manifest; empty when there is no manifest."""
    if not MANIFEST.exists():
        return {}
    files = json.loads(MANIFEST.read_text(encoding="utf-8"))["files"]
    return {name: entry["sha256"] for name, entry in files.items()}


def _is_current(dest: Path, expected: str | None) -> bool:
    if not dest.exists():
        return False
    # With a manifest, only an exact match counts: a file fetched earlier from
    # another upstream revision (e.g. master) must be replaced, not kept.
    if expected is not None:
        return _sha256(dest) == expected
    return dest.stat().st_size > 100


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    expected = _expected_hashes()
    for name in FILES:
        dest = OUT / name
        if _is_current(dest, expected.get(name)):
            print("skip", name, dest.stat().st_size)
            continue
        print("GET", name)
        urllib.request.urlretrieve(BASE + name, dest)
        print(" ", dest.stat().st_size)


if __name__ == "__main__":
    main()
