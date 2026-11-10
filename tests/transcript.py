"""One JSON file = one timestamped IME protocol transcript.

Expectations include every visible field after *each* event, plus the document
formed by concatenating commits. Tests never update their own expectations.
"""

from __future__ import annotations

import json
from pathlib import Path

from oyayubi.ime.server import dispatch
from oyayubi.ime.session import ImeSession
from tools.wp0.mozc_dict import Entry, MozcLex
from tools.wp0.table import load_table

VISIBLE = {"composition", "candidates", "cand_index", "converted", "commit", "committed"}
SEGMENT_VISIBLE = {"segments", "focus", "candidate_page", "candidate_kinds", "has_next_page"}


def make_session(case: dict, real_lex: MozcLex | None = None) -> ImeSession:
    if "lexicon" not in case:
        if real_lex is None:
            raise ValueError("a transcript without lexicon needs the real dictionary")
        lex = real_lex
    else:
        by_reading: dict[str, list[Entry]] = {}
        prefixes: set[str] = set()
        for row in case["lexicon"]:
            reading, surface = row["reading"], row["surface"]
            entry = Entry(surface, reading, row.get("lid", 1), row.get("rid", 1),
                          row.get("cost", 100))
            by_reading.setdefault(reading, []).append(entry)
            prefixes.update(reading[:i] for i in range(1, len(reading) + 1))
        matrix = case.get("connection", [[0, 0], [0, 0]])
        if isinstance(matrix, dict):
            size = matrix["size"]
            costs = [matrix.get("default", 0)] * (size * size)
            for rid, lid, cost in matrix.get("overrides", []):
                costs[rid * size + lid] = cost
        else:
            size = len(matrix)
            costs = [cost for row in matrix for cost in row]
        lex = MozcLex(by_reading, prefixes, costs, size)
    return ImeSession(_table=load_table(), _lex=lex, learning_path=None)


def run_transcript(test, path: Path, real_lex: MozcLex | None = None) -> None:
    case = json.loads(path.read_text(encoding="utf-8"))
    test.assertIn(case["version"], (1, 2))
    session = make_session(case, real_lex)
    committed = ""
    previous_time = -1
    for index, step in enumerate(case["steps"]):
        event, expected = step["event"], step["expect"]
        with test.subTest(file=path.name, step=index + 1, event=event, note=step.get("note")):
            test.assertGreaterEqual(event["t"], previous_time)
            previous_time = event["t"]
            test.assertTrue(VISIBLE <= expected.keys(), "all visible fields must be explicit")
            if case["version"] == 2:
                test.assertTrue(SEGMENT_VISIBLE <= expected.keys(), "segment state must be explicit")
            reply = dispatch(session, event)
            # A real transport serializes immediately; capture candidate lists now.
            committed += reply["commit"]
            actual = {**reply, "committed": committed}
            test.assertEqual({key: actual[key] for key in expected}, expected)
    test.assertEqual(committed, case["committed"])
    test.assertEqual(session.composition, case["composition"])
