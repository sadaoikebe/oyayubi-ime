"""NICOLA-A face table."""

from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data" / "nicola_a.json"


def load_table(path: Path | None = None) -> dict:
    p = path or DATA
    with p.open(encoding="utf-8") as f:
        return json.load(f)
