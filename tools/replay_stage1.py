"""Replay Stage 1 raw keys through the frozen ChordFsm, without Mozc/Windows.

Run: python3 -m tools.replay_stage1 recording.jsonl [--json] [--report report.json]
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
from pathlib import Path

from yuragi.chord.fsm import AmbShift, ChordFsm, Plain, ThumbTap
from yuragi.chord.keys import (
    SHIFT_VKS, SHORTCUT_VKS, VK_BACK, VK_ESCAPE, VK_RETURN, VK_SPACE,
    classify, literal_character,
)
from yuragi.ime.recording import LEGACY_NEXT_PROMPT_VK, SCHEMA, VK_NEXT_PROMPT
from yuragi.lattice.table import load_table

LABELS = {
    "thumb_overlap_shift_loss": "親指の重なりでシフト消失（推定）",
    "unintended_chord": "意図しない同時打鍵",
    "missed_chord": "同時打鍵の取りこぼし",
    "other": "その他",
}


@dataclass(frozen=True)
class Native:
    text: str


def token_json(token) -> dict:
    if isinstance(token, Native):
        return {"kind": "literal", "text": token.text}
    return {"kind": type(token).__name__, "key": token.key}


def raw_token_json(o: "Observation") -> dict:
    """token_json plus where the stroke was typed (press/turn, held in a used press)."""
    row = dict(token_json(o.token), ms=o.ms, source_event=o.source_event)
    if isinstance(o.token, AmbShift):
        row.update(press=o.token.press, turn=o.token.turn)
    if o.held:
        row["held"] = True
    return row


def expected_tokens(reading: str, table: dict | None = None) -> list:
    """Plain faces first; shifted kana/literals keep the physical AmbShift key.

    Reading is the actual string to type: hiragana + punctuation + ASCII digits.
    In particular は/へ/を are spelling, not phonetic わ/え/お. No characters are
    silently skipped, and conversion keys are not inferred from the text.
    """
    table = table or load_table()
    reverse = {}
    for face in ("plain", "left", "right"):
        for key, faces in table["keys"].items():
            text = faces.get(face)
            if text:
                text = "、" if text == "，" else text
                reverse.setdefault(text, Plain(key) if face == "plain" else AmbShift(key))
    result = []
    for char in reading:
        if char in reverse:
            result.append(reverse[char])
        elif char in "0123456789":
            result.append(Native(char))
        else:
            raise ValueError(f"unencodable prompt character: {char!r}")
    return result


@dataclass
class Trace:
    prompt: dict
    events: list[dict] = field(default_factory=list)
    status: str = "incomplete"
    end_ms: int = 0
    next_prompt_vk: int = VK_NEXT_PROMPT  # the log's own key (session row)


def read_log(path: Path) -> tuple[list[Trace], bool]:
    """Reject malformed/cross-prompt/unordered data; preserve interrupted traces."""
    traces = []
    current = None
    metadata = None
    closed = False
    last_ms = -1
    ids = set()
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("row must be an object")
                ms = row["ms"]
                if type(ms) is not int or ms < 0 or ms < last_ms:
                    raise ValueError("ms must be nonnegative, nondecreasing integer")
                last_ms = ms
                if closed:
                    raise ValueError("data after session_end")
                kind = row["type"]
                if metadata is None and kind != "session":
                    raise ValueError("first row must be session metadata")
                if kind == "session":
                    if metadata is not None or row["schema"] != SCHEMA:
                        raise ValueError("duplicate session or unsupported schema")
                    if row["clock"] != "QPC" or row["time_unit"] != "ms":
                        raise ValueError("unsupported clock/unit")
                    if row["layout"] != "NICOLA-A":
                        raise ValueError("unsupported keyboard layout")
                    metadata = row
                elif kind == "prompt_start":
                    if current is not None or row["id"] in ids:
                        raise ValueError("overlapping or repeated prompt")
                    if any(not isinstance(row[k], str) or not row[k]
                           for k in ("id", "text", "reading")):
                        raise ValueError("empty/invalid prompt")
                    expected_tokens(row["reading"])
                    ids.add(row["id"])
                    current = Trace(row, next_prompt_vk=metadata.get(
                        "next_prompt_vk", LEGACY_NEXT_PROMPT_VK))
                    traces.append(current)
                elif kind == "key":
                    if current is None or row["id"] != current.prompt["id"]:
                        raise ValueError("key outside its prompt")
                    if (type(row["vk"]) is not int or not 0 <= row["vk"] <= 255
                            or row["action"] not in ("down", "up")):
                        raise ValueError("invalid vk/action")
                    current.events.append(row)
                    current.end_ms = ms
                elif kind == "prompt_end":
                    if current is None or row["id"] != current.prompt["id"]:
                        raise ValueError("prompt_end without matching prompt_start")
                    if row["status"] not in ("complete", "interrupted", "incomplete"):
                        raise ValueError("invalid prompt status")
                    if row["event_count"] != len(current.events):
                        raise ValueError("event_count mismatch")
                    if row["status"] == "complete" and row["held_vks"]:
                        raise ValueError("complete prompt has held keys")
                    current.status, current.end_ms = row["status"], ms
                    current = None
                elif kind == "focus":
                    if type(row["focused"]) is not bool:
                        raise ValueError("invalid focus state")
                elif kind == "session_end":
                    if current is not None:
                        raise ValueError("session_end inside a prompt")
                    closed = True
                else:
                    raise ValueError(f"unknown row type: {kind}")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{number}: {exc}") from exc
    if metadata is None:
        raise ValueError("empty log")
    return traces, closed


@dataclass
class Observation:
    token: object
    ms: int
    source_event: int
    held_consumed_space: bool = False
    held: bool = False  # a character typed inside a used press (left the FSM's S7)


def replay(trace: Trace, fsm=None) -> tuple[list[Observation], dict]:
    """Fire due deadlines BEFORE equal-time keys; drain pending at prompt end.

    This is a deterministic virtual clock, not a reconstruction of WM_TIMER
    scheduling. Native digits/edit boundaries finish pending FSM output using
    its existing timeout path, as the session does, but never delete tokens.
    fsm: another state machine with the same interface (default ChordFsm()).
    """
    fsm = fsm if fsm is not None else ChordFsm()
    observations = []
    modifiers: set[int] = set()
    native_down: set[int] = set()
    physical: set[int] = set()
    sources: dict[str, tuple[int, bool]] = {}
    space_used = False
    counts = Counter()

    def emit(tokens, ms, held=False):
        """held: the FSM was in S7 before the event; every S7 exit emits its m."""
        nonlocal space_used
        for token in tokens:
            source, held_used = sources.get(token.key, (-1, False))
            observations.append(Observation(token, ms, source, held_used, held))
            if held:
                counts[f"held_{type(token).__name__}"] += 1
            if isinstance(token, AmbShift):
                space_used = True

    def timeout(ms):
        deadline = fsm.timer_deadline
        if deadline is not None and deadline <= ms:
            held = fsm.state == "S7"
            emit(fsm.on_timeout(deadline), deadline, held)

    def boundary(ms):
        if fsm.timer_deadline is not None:
            held = fsm.state == "S7"
            emit(fsm.on_timeout(fsm.timer_deadline), ms, held)
        fsm.reset()

    for index, event in enumerate(trace.events):
        ms, vk = event["ms"], event["vk"]
        down = event["action"] == "down"
        timeout(ms)
        repeat = down and vk in physical
        if down:
            physical.add(vk)
        else:
            physical.discard(vk)
        kind, key = classify(vk)
        if kind == "MOD":
            if down:
                modifiers.add(vk)
            else:
                modifiers.discard(vk)
            continue
        if vk in native_down:
            if not down:
                native_down.discard(vk)
            continue
        if repeat:
            counts["repeat_downs"] += 1
            continue
        if vk == trace.next_prompt_vk:
            if down:
                counts["next_prompt_downs"] += 1
            continue
        if modifiers & SHORTCUT_VKS:
            if down:
                counts["shortcut_downs"] += 1
            continue
        if vk in (VK_BACK, VK_ESCAPE, VK_RETURN):
            if down:
                boundary(ms)
                counts[{VK_BACK: "backspace_downs", VK_ESCAPE: "escape_downs",
                        VK_RETURN: "enter_downs"}[vk]] += 1
            continue
        native = literal_character(vk, bool(modifiers & SHIFT_VKS))
        if native is not None:
            if down:
                boundary(ms)
                native_down.add(vk)
                observations.append(Observation(Native(native), ms, index))
            continue
        if kind not in ("M", "O"):
            if down:
                counts["other_key_downs"] += 1
            continue
        if down:
            # on_down can settle the PREVIOUS character. Register this source
            # after processing it so even repeated same-key strokes keep origin.
            held = fsm.state == "S7"
            emit(fsm.on_down(kind, key, ms), ms, held)
            if kind == "O":
                space_used = False
            sources[key] = (index, VK_SPACE in physical and space_used)
        else:
            held = fsm.state == "S7"
            emit(fsm.on_up(kind, key, ms), ms, held)
    boundary(trace.end_ms)
    counts["thumb_taps"] = sum(isinstance(o.token, ThumbTap) for o in observations)
    return observations, dict(counts)


def align(expected: list, actual: list[Observation]) -> list[tuple[str, int | None, int | None]]:
    """Unit-cost Levenshtein, deterministic match/substitute/delete/insert ties."""
    n, m = len(expected), len(actual)
    costs = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        costs[i][0] = i
    for j in range(m + 1):
        costs[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            costs[i][j] = min(costs[i - 1][j] + 1, costs[i][j - 1] + 1,
                              costs[i - 1][j - 1] + (expected[i - 1] != actual[j - 1].token))
    result = []
    i, j = n, m
    while i or j:
        equal = bool(i and j and expected[i - 1] == actual[j - 1].token)
        if i and j and costs[i][j] == costs[i - 1][j - 1] + (not equal):
            result.append(("match" if equal else "substitution", i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i and costs[i][j] == costs[i - 1][j] + 1:
            result.append(("deletion", i - 1, None))
            i -= 1
        else:
            result.append(("insertion", None, j - 1))
            j -= 1
    return list(reversed(result))


def evaluate_trace(trace: Trace) -> dict:
    expected = expected_tokens(trace.prompt["reading"])
    observations, controls = replay(trace)
    # Standalone conversion Space has no prescribed position in the prompt's
    # reading. Retain it in raw_tokens and report it separately, not as an error.
    actual = [o for o in observations if not isinstance(o.token, ThumbTap)]
    operations = Counter()
    failures = Counter({key: 0 for key in LABELS})
    errors = []
    for operation, i, j in align(expected, actual):
        operations[operation] += 1
        if operation == "match":
            continue
        want = expected[i] if i is not None else None
        got = actual[j] if j is not None else None
        category = "other"
        if want is not None and got is not None:
            token = got.token
            if isinstance(want, Plain) and isinstance(token, AmbShift) and want.key == token.key:
                category = "unintended_chord"
            elif isinstance(want, AmbShift) and isinstance(token, Plain) and want.key == token.key:
                category = ("thumb_overlap_shift_loss" if got.held_consumed_space else "missed_chord")
        failures[category] += 1
        errors.append({"operation": operation, "category": category,
                       "expected_index": i, "actual_index": j,
                       "expected": token_json(want) if want is not None else None,
                       "actual": token_json(got.token) if got is not None else None,
                       "source_event": got.source_event if got is not None else None,
                       "ms": got.ms if got is not None else None})
    edits = len(errors)
    return {"id": trace.prompt["id"], "text": trace.prompt["text"],
            "reading": trace.prompt["reading"], "expected_count": len(expected),
            "actual_count": len(actual), "matches": operations["match"],
            "substitutions": operations["substitution"], "deletions": operations["deletion"],
            "insertions": operations["insertion"], "edit_distance": edits,
            "token_accuracy": max(0.0, 1 - edits / len(expected)),
            "failures": dict(failures), "controls": controls,
            "expected_tokens": [token_json(t) for t in expected],
            "raw_tokens": [raw_token_json(o) for o in observations], "errors": errors}


def evaluate_log(path: Path) -> dict:
    traces, closed = read_log(path)
    scored = [evaluate_trace(t) for t in traces if t.status == "complete"]
    totals = {key: sum(row[key] for row in scored) for key in (
        "expected_count", "actual_count", "matches", "substitutions", "deletions",
        "insertions", "edit_distance")}
    n = totals["expected_count"]
    totals["token_accuracy"] = max(0.0, 1 - totals["edit_distance"] / n) if n else None
    totals["failures"] = {key: sum(row["failures"][key] for row in scored) for key in LABELS}
    controls = Counter()
    for row in scored:
        controls.update(row["controls"])
    totals["controls"] = dict(controls)
    return {"schema": "oyayubi.stage1.report.v1", "log": str(path),
            "session_closed": closed, "completed_prompts": len(scored),
            "excluded_prompts": [{"id": t.prompt["id"], "status": t.status}
                                 for t in traces if t.status != "complete"],
            "summary": totals, "prompts": scored}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--report", type=Path, help="save full JSON report (contains prompt text)")
    args = parser.parse_args()
    try:
        report = evaluate_log(args.log)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.write_text(serialized, encoding="utf-8")
    if args.json:
        print(serialized, end="")
    else:
        summary = report["summary"]
        accuracy = summary["token_accuracy"]
        print(f"完了文: {report['completed_prompts']} / 除外文: {len(report['excluded_prompts'])}")
        print("トークン正解率: " + (f"{accuracy:.2%}" if accuracy is not None else "採点対象なし"))
        print(f"期待 {summary['expected_count']} / 出力 {summary['actual_count']} / "
              f"一致 {summary['matches']} / S {summary['substitutions']} / "
              f"D {summary['deletions']} / I {summary['insertions']}")
        for key, label in LABELS.items():
            print(f"  {label}: {summary['failures'][key]}")
        print(f"操作・Space単独（採点外）: {summary['controls']}")
        if not report["session_closed"]:
            print("注意: session_end なし。末尾の未完了文は採点から除外。")
    return 0 if report["completed_prompts"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
