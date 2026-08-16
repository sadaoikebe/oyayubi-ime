"""Download Mozc OSS dictionary text (not a full Mozc build)."""

from __future__ import annotations

import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "third_party" / "mozc_oss_dict"
BASE = "https://raw.githubusercontent.com/google/mozc/master/src/data/dictionary_oss/"
FILES = (
    ["README.txt", "README.md", "id.def", "suffix.txt", "evaluation.tsv"]
    + [f"dictionary{i:02d}.txt" for i in range(10)]
    + ["connection_single_column.txt"]
)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        dest = OUT / name
        if dest.exists() and dest.stat().st_size > 100:
            print("skip", name, dest.stat().st_size)
            continue
        print("GET", name)
        urllib.request.urlretrieve(BASE + name, dest)
        print(" ", dest.stat().st_size)


if __name__ == "__main__":
    main()
