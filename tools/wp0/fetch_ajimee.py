"""Fetch AJIMEE-Bench JWTD_v2/v1 (CC BY-SA 3.0); no data is bundled.

Run: python3 -m tools.wp0.fetch_ajimee
See docs/EVALUATION.md for attribution, license and evaluation limits.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "cache" / "ajimee"
DATA = OUT / "evaluation_items.json"
REVISION = "401666cd56d1a570c2021798b64b6da4396bfd45"
BASE = f"https://raw.githubusercontent.com/azooKey/AJIMEE-Bench/{REVISION}/"


def main() -> None:
    # Keep the upstream license/attribution beside the unmodified data.
    files = {"README.md": "README.md", "evaluation_items.json": "JWTD_v2/v1/evaluation_items.json"}
    OUT.mkdir(parents=True, exist_ok=True)
    for name, upstream in files.items():
        with urllib.request.urlopen(BASE + upstream, timeout=60) as response:
            data = response.read()
        if name.endswith(".json"):
            items = json.loads(data)
            if not isinstance(items, list) or len(items) != 200:
                raise ValueError("Expected the pinned 200-item AJIMEE dataset")
        else:
            if "CC-BY-SA 3.0" not in data.decode("utf-8"):
                raise ValueError("Upstream license notice changed")
        (OUT / name).write_bytes(data)
        print(f"{name}: {len(data)} bytes; sha256={hashlib.sha256(data).hexdigest()}")
    print(f"AJIMEE-Bench {REVISION}: CC BY-SA 3.0; downloaded to {OUT}")


if __name__ == "__main__":
    main()
