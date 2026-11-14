"""NICOLA-A face table."""

from __future__ import annotations

import json
from pathlib import Path

from .expand import is_literal_face

DATA = Path(__file__).resolve().parents[2] / "data" / "nicola_a.json"


def load_table(path: Path | None = None) -> dict:
    p = path or DATA
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def reverse_table(table: dict) -> dict[str, dict]:
    """Map each face to the lattice token produced by its key."""
    result = {}
    # Plain faces take precedence if a kana appears on multiple keys.
    for key, faces in table["keys"].items():
        kana = faces.get("plain")
        # Evaluation encodes kana readings only; punctuation is handled by the
        # IME as a literal boundary, not as a kana dictionary arc.
        if kana and not is_literal_face(kana):
            result.setdefault(kana, {"kind": "plain", "key": key, "kana": kana})
    for key, faces in table["keys"].items():
        shifts = {face: faces[face] for face in ("left", "right") if faces.get(face)}
        token = {"kind": "amb" if len(shifts) > 1 else "shift", "key": key, "faces": shifts}
        for kana in shifts.values():
            if not is_literal_face(kana):
                result.setdefault(kana, token)
    return result
