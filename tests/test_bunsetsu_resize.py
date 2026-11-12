from __future__ import annotations

import unittest

from oyayubi.chord.fsm import Plain
from oyayubi.ime.server import dispatch
from oyayubi.ime.session import Literal
from tests.transcript import make_session


def session(reading, rows):
    s = make_session({"lexicon": [
        {"reading": r, "surface": text, "cost": cost, "lid": 12, "rid": 12}
        for r, text, cost in rows], "connection": {"size": 13}})
    s._table = {"keys": {c: {"plain": c} for c in reading}}
    s.tokens = [Plain(c) for c in reading]
    s._refresh()
    return s


def tap(s, vk, mods=None):
    event = {"vk": vk, "mods": mods or []}
    reply = dispatch(s, {"op": "down", **event})
    dispatch(s, {"op": "up", **event})
    return reply


def intervals(s):
    return [(seg.start, seg.end) for seg in s.segments]


class BunsetsuResizeTest(unittest.TestCase):
    def test_transfer_to_and_from_long_following_segment(self):
        s = session("かたく", [("か", "蚊", 0), ("たく", "宅", 0),
                              ("た", "田", 10), ("く", "区", 10), ("かた", "肩", 20)])
        tap(s, 32)
        tap(s, 39)
        tap(s, 49)  # Fix 宅; its interval changes and must be recomputed too.
        tap(s, 37)
        tap(s, 39, ["shift"])
        self.assertEqual(intervals(s), [(0, 2), (2, 3)])
        self.assertEqual(s.composition, "蚊田区")
        self.assertEqual([seg.fixed for seg in s.segments], [False, False])
        self.assertIn("肩", s.candidates)
        tap(s, 50)
        self.assertEqual(s.composition, "肩区")
        tap(s, 37, ["shift"])
        self.assertEqual(intervals(s), [(0, 1), (1, 3)])
        self.assertEqual(s.composition, "蚊宅")
        self.assertEqual(s.focus, 0)
        tap(s, 39)
        self.assertEqual(s.candidates, ["宅", "田区", "たく", "タク"])

    def test_literal_barrier_and_shrinking_before_it(self):
        s = session("かたく", [("かた", "肩", 0), ("か", "蚊", 10),
                              ("た", "田", 10), ("く", "区", 0)])
        s.tokens.insert(2, Literal("!"))
        s._refresh()
        tap(s, 32)
        literal, right = s.segments[1:]
        before = s.segment_snapshot()
        tap(s, 39, ["shift"])
        self.assertEqual(s.segment_snapshot(), before)
        tap(s, 37, ["shift"])
        self.assertEqual(intervals(s), [(0, 1), (1, 2), (2, 3), (3, 4)])
        self.assertEqual(s.composition, "蚊田!区")
        self.assertIs(s.segments[2], literal)
        self.assertIs(s.segments[3], right)
        tap(s, 39, ["shift"])
        self.assertEqual(s.segment_snapshot(), before)
        tap(s, 39)  # Ordinary movement still skips the literal.
        self.assertEqual(s.focus, 2)

    def test_cancel_discards_override(self):
        for operation in ("escape", "backspace", "reset", "direct"):
            with self.subTest(operation=operation):
                s = session("かた", [("か", "蚊", 0), ("た", "田", 0), ("かた", "肩", 10)])
                automatic = s.segment_snapshot()
                tap(s, 32)
                tap(s, 39, ["shift"])
                tap(s, 50)
                self.assertEqual(s.composition, "肩")
                if operation in ("escape", "backspace"):
                    tap(s, 27 if operation == "escape" else 8)
                    self.assertEqual(s.segment_snapshot(), automatic)
                    tap(s, 32)
                    self.assertEqual(intervals(s), [(0, 1), (1, 2)])
                else:
                    dispatch(s, {"op": "reset"} if operation == "reset"
                             else {"op": "set_direct", "direct": True})
                    self.assertEqual((s.segments, s.focus, s.candidates), ([], -1, []))
                    self.assertFalse(s.converted)

    def test_tracked_shift_repeat_release_and_shortcut(self):
        s = session("かたく", [("か", "蚊", 0), ("た", "田", 0), ("く", "区", 0)])
        tap(s, 39, ["shift"])  # A focus during input alone does not enable resizing.
        self.assertEqual(intervals(s), [(0, 1), (1, 2), (2, 3)])
        tap(s, 32)
        tap(s, 39, ["shift", "ctrl"])
        self.assertEqual(intervals(s), [(0, 1), (1, 2), (2, 3)])
        dispatch(s, {"op": "down", "vk": 16})
        dispatch(s, {"op": "down", "vk": 39})
        self.assertEqual(intervals(s), [(0, 2), (2, 3)])
        dispatch(s, {"op": "down", "vk": 39})  # HID repeat is consumed.
        dispatch(s, {"op": "up", "vk": 16})
        dispatch(s, {"op": "up", "vk": 39})
        self.assertEqual((intervals(s), s.focus), ([(0, 2), (2, 3)], 0))
        tap(s, 39)
        self.assertEqual(s.focus, 1)
        tap(s, 37)
        self.assertEqual(intervals(s), [(0, 2), (2, 3)])

    def test_no_focus_with_only_literals(self):
        s = make_session({"lexicon": []})
        tap(s, 49, ["shift"])  # "!"
        tap(s, 32)
        self.assertTrue(s.converted)
        self.assertEqual(s.focus, -1)
        before = s.segment_snapshot()
        for vk in (37, 39):
            tap(s, vk, ["shift"])
            self.assertEqual(s.segment_snapshot(), before)

    def test_digit_run_is_focusable_but_keeps_its_interval(self):
        s = make_session({"lexicon": []})
        for vk in (49, 50):
            tap(s, vk)
        tap(s, 32)
        self.assertEqual((intervals(s), s.focus), ([(0, 2)], 0))
        self.assertEqual(s.candidates[0], "12")
        self.assertIn("十二", s.candidates)
        before = s.segment_snapshot()
        for vk in (37, 39):
            tap(s, vk, ["shift"])
            self.assertEqual(s.segment_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
