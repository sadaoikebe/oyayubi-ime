"""Golden traces from PLAN PR-02 / NICOLA-SPEC §4."""

from __future__ import annotations

import unittest

from oyayubi.chord.fsm import AmbShift, ChordFsm, Plain, ThumbTap


def run(events: list[tuple[str, str, str, int]]):
    """events: (down|up|to, kind, key, t_ms)."""
    fsm = ChordFsm()
    got = []
    for op, kind, key, t in events:
        if op == "down":
            got.extend(fsm.on_down(kind, key, t))
        elif op == "up":
            got.extend(fsm.on_up(kind, key, t))
        elif op == "to":
            fsm.timer_deadline = t  # fire now
            got.extend(fsm.on_timeout(t))
    return got, fsm.state


class FsmGolden(unittest.TestCase):
    def test_prefix_chord(self):
        # Both-down must last >= OVERLAP (20ms) or the first key is a tap.
        toks, st = run(
            [
                ("down", "O", "space", 0),
                ("down", "M", "j", 60),
                ("up", "O", "space", 90),
                ("up", "M", "j", 100),
            ]
        )
        self.assertEqual(toks, [AmbShift("j")])
        self.assertEqual(st, "S1")

    def test_postfix_chord(self):
        toks, st = run(
            [
                ("down", "M", "j", 0),
                ("down", "O", "space", 60),
                ("up", "M", "j", 90),
                ("up", "O", "space", 100),
            ]
        )
        self.assertEqual(toks, [AmbShift("j")])
        self.assertEqual(st, "S1")

    def test_char_up_before_thumb(self):
        toks, st = run(
            [
                ("down", "M", "j", 0),
                ("up", "M", "j", 40),
            ]
        )
        self.assertEqual(toks, [Plain("j")])
        self.assertEqual(st, "S1")

    def test_second_char_settles_first(self):
        toks, _ = run(
            [
                ("down", "M", "j", 0),
                ("down", "M", "h", 40),
            ]
        )
        self.assertEqual(toks, [Plain("j")])

    def test_three_key_t1_ge_t2(self):
        # J@0, Space@50, H@80 → t1=50 t2=30 → Plain(J), S5
        toks, st = run(
            [
                ("down", "M", "j", 0),
                ("down", "O", "space", 50),
                ("down", "M", "h", 80),
            ]
        )
        self.assertEqual(toks, [Plain("j")])
        self.assertEqual(st, "S5")

    def test_space_tap(self):
        toks, st = run(
            [
                ("down", "O", "space", 0),
                ("up", "O", "space", 40),
            ]
        )
        self.assertEqual(toks, [ThumbTap()])
        self.assertEqual(st, "S1")

    def test_space_timeout_is_standalone_then_j_is_to(self):
        # Space が窓を越えたら単独。あとから J は「と」であり AmbShift ではない。
        toks, st = run(
            [
                ("down", "O", "space", 0),
                ("to", "O", "space", 80),
                ("down", "M", "j", 100),
                ("up", "M", "j", 140),
                ("up", "O", "space", 150),
            ]
        )
        self.assertEqual(toks, [ThumbTap(), Plain("j")])
        self.assertEqual(st, "S1")

    def test_timeout_plain_preview(self):
        toks, st = run(
            [
                ("down", "M", "j", 0),
                ("to", "M", "j", 80),
            ]
        )
        self.assertEqual(toks, [Plain("j")])
        self.assertEqual(st, "S1")

    def test_timeout_then_release_no_dup(self):
        toks, st = run(
            [
                ("down", "M", "j", 0),
                ("to", "M", "j", 80),
                ("up", "M", "j", 100),
            ]
        )
        self.assertEqual(toks, [Plain("j")])
        self.assertEqual(st, "S1")

    def test_repeat_suppressed(self):
        fsm = ChordFsm()
        self.assertEqual(fsm.on_down("M", "j", 0), [])
        self.assertEqual(fsm.on_down("M", "j", 30), [])

    def test_ohayou_sequence(self):
        # Space+J, H, Space+Y, A  (right-thumb intent, but Space is unknown)
        toks, _ = run(
            [
                ("down", "O", "space", 0),
                ("down", "M", "j", 40),
                ("up", "O", "space", 70),
                ("up", "M", "j", 80),
                ("down", "M", "h", 140),
                ("up", "M", "h", 180),
                ("down", "O", "space", 220),
                ("down", "M", "y", 260),
                ("up", "O", "space", 290),
                ("up", "M", "y", 300),
                ("down", "M", "a", 300),
                ("up", "M", "a", 340),
            ]
        )
        self.assertEqual(
            toks,
            [AmbShift("j"), Plain("h"), AmbShift("y"), Plain("a")],
        )


if __name__ == "__main__":
    unittest.main()
