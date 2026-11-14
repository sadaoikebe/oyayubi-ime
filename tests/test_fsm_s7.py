"""S7 (a character held inside a used press) and press/turn numbering.

Album design 2 (2026-11-14) §3.2/§3.5/§4.1, on the S1–S7 table of fsm-v2.
"""

from __future__ import annotations

import os
import random
import unittest
from pathlib import Path

from yuragi.chord.fsm import AmbShift, ChordFsm, Plain, ThumbTap
from tests import legacy_fsm
from tools.replay_stage1 import read_log, replay

ROOT = Path(__file__).resolve().parents[1]
# The 2026-11-11 laptop recording (40 sentences). data/recordings/ is not in
# git (the user's own keystrokes); the tests that need it skip without it.
STAGE1_LOG = Path(os.environ.get(
    "OYAYUBI_STAGE1_LOG", ROOT / "data" / "recordings" / "stage1-20261111-213449.jsonl"))


def run(events, fsm=None):
    """events: "U+0" (down), "U-102" (up), "S" is Space. Due deadlines fire
    first, as in the session and the replay. Returns (tokens, fsm)."""
    fsm = fsm or ChordFsm()
    out = []
    for ev in events:
        key, op, t = ev[0], ev[1], int(ev[2:])
        kind, key = ("O", "space") if key == "S" else ("M", key.lower())
        if fsm.timer_deadline is not None and fsm.timer_deadline <= t:
            out += fsm.on_timeout(fsm.timer_deadline)
        out += (fsm.on_down if op == "+" else fsm.on_up)(kind, key, t)
    if fsm.timer_deadline is not None:
        out += fsm.on_timeout(fsm.timer_deadline)
    return out, fsm


def shape(tokens):
    """Tokens with the press numbering, which AmbShift equality ignores."""
    return [(type(t).__name__, t.key) + ((t.press, t.turn) if isinstance(t, AmbShift) else ())
            for t in tokens]


def shifted(t: int, events: str) -> list[str]:
    """Write a §4.1 time line relative to Space down; shift it to start at t."""
    return [e[:2] + str(int(e[2:]) + t) for e in events.split()]


class Examples(unittest.TestCase):
    """design2 §3.5."""

    def test_nyu_real(self):
        # U↓0 S↓21 U↑102 F↓140 S↑205 F↑225: cover 65 >= U 102 - 65.
        toks, fsm = run("U+0 S+21 U-102 F+140 S-205 F-225".split())
        self.assertEqual(shape(toks), [("AmbShift", "u", 1, 1), ("AmbShift", "f", 1, 2)])
        self.assertEqual(fsm.state, "S1")

    def test_ryo_real(self):
        # S↓0 E↓20 E↑103 L↓124 (deadline 204) L↑207 S↑228.
        toks, fsm = run("S+0 E+20 E-103 L+124 L-207 S-228".split())
        self.assertEqual(shape(toks), [("AmbShift", "e", 1, 1), ("AmbShift", "l", 1, 2)])
        self.assertEqual(fsm.state, "S1")

    def test_yuu_late_release(self):
        # A U stroke of 82 ms before; F↓-21 S↓0 A↓70 S↑90: cover 20 < 82 - 20.
        toks, _ = run("U+0 U-82 F+179 S+200 A+270 S-290 F-290 A-334".split())
        self.assertEqual(shape(toks), [("Plain", "u"), ("AmbShift", "f", 1, 1), ("Plain", "a")])

    def test_ou_short_cover_returns_to_s2(self):
        # S↓0 J↓10 J↑50 A↓60 S↑70: cover 10 < V, A stays pending (S2).
        fsm = ChordFsm()
        out = []
        for ev in "S+0 J+10 J-50 A+60 S-70".split():
            key, op, t = ev[0], ev[1], int(ev[2:])
            kind, k = ("O", "space") if key == "S" else ("M", key.lower())
            out += (fsm.on_down if op == "+" else fsm.on_up)(kind, k, t)
        self.assertEqual(fsm.state, "S2")
        self.assertEqual(fsm.timer_deadline, 140)  # A down + T, not re-armed
        out += fsm.on_up("M", "a", 100)
        self.assertEqual(shape(out), [("AmbShift", "j", 1, 1), ("Plain", "a")])

    def test_original_album_in_one_press(self):
        # J E S D I S I H , all inside one Space press, each released in it.
        keys = "jesdisih,"
        events = ["S+0"]
        t = 10
        for k in keys:
            events += [f"{k}+{t}", f"{k}-{t + 50}"]
            t += 70
        events.append(f"S-{t}")
        toks, fsm = run(events)
        self.assertEqual(shape(toks), [("AmbShift", k, 1, i) for i, k in enumerate(keys, 1)])
        self.assertEqual(fsm.state, "S1")


class S7Exits(unittest.TestCase):
    """Each S7 exit of design2 §3.2."""

    def chord_then(self, rest: str):
        # J chord (S↓0 J↓10 J↑90, last length 80), then m inside the press.
        return run(("S+0 J+10 J-90 " + rest).split())[0]

    def test_space_up_compares_cover_with_last_length(self):
        # last 80: chord iff cover >= 40.
        self.assertEqual(self.chord_then("K+100 S-140 K-150")[1], AmbShift("k"))
        self.assertEqual(self.chord_then("K+100 S-139 K-200")[1], Plain("k"))
        self.assertEqual(self.chord_then("K+100 S-120 K-200")[1], Plain("k"))  # V <= cover

    def test_char_up_is_a_chord(self):
        toks = self.chord_then("K+100 K-110 S-200")
        self.assertEqual(shape(toks)[1], ("AmbShift", "k", 1, 2))

    def test_deadline_is_a_chord(self):
        toks, fsm = run("S+0 J+10 J-90 K+100".split())
        self.assertEqual(shape(toks)[1], ("AmbShift", "k", 1, 2))
        self.assertEqual(fsm.state, "S6")

    def test_next_char_compares_cover_so_far(self):
        # K↓100, L↓140: cover 40 >= 80 - 40, a chord; L is the next pending.
        toks = self.chord_then("K+100 L+140 K-150 L-160 S-250")
        self.assertEqual(shape(toks)[1:], [("AmbShift", "k", 1, 2), ("AmbShift", "l", 1, 3)])
        # L↓130: cover 30 < 50, plain; the plain does not take a turn.
        toks = self.chord_then("K+100 L+130 K-150 L-160 S-250")
        self.assertEqual(shape(toks)[1:], [("Plain", "k"), ("AmbShift", "l", 1, 2)])

    def test_no_last_length_uses_t(self):
        # First strokes ever: J and K overlap, so no character was released yet.
        toks, _ = run("S+0 J+10 K+95 S-130 J-140 K-200".split())
        # J: S5 deadline 90 → chord. K: cover 35 < T 80 - 35 → plain.
        self.assertEqual(shape(toks), [("AmbShift", "j", 1, 1), ("Plain", "k")])
        toks, _ = run("S+0 J+10 K+95 S-136 J-140 K-200".split())
        self.assertEqual(shape(toks), [("AmbShift", "j", 1, 1), ("AmbShift", "k", 1, 2)])

    def test_presses_are_numbered(self):
        toks, _ = run("S+0 J+10 J-50 S-60 K+200 S+220 K-260 S-280 S+400 S-430".split())
        self.assertEqual(shape(toks), [("AmbShift", "j", 1, 1), ("AmbShift", "k", 2, 1),
                                       ("ThumbTap", "space")])


class AfterLoneSpace(unittest.TestCase):
    """A character held in a press whose job was a lone Space is plain
    (design2 §6.2 is open; the code keeps today's output)."""

    EVENTS = "G+0 G-113 S+300 E+443 S-518 E-539"  # sentence 10, G 113 ms

    def test_held_character_is_plain(self):
        toks, _ = run(self.EVENTS.split())
        self.assertEqual(shape(toks), [("Plain", "g"), ("ThumbTap", "space"), ("Plain", "e")])

    def test_existing_space_timeout_case(self):
        # tests/test_fsm.py test_space_timeout_is_standalone_then_j_is_to.
        events = "S+0 J+100 J-140 S-150".split()
        self.assertEqual(run(events)[0], [ThumbTap(), Plain("j")])


class RealHeldCharacters(unittest.TestCase):
    """The six characters held in a used press in the 11/11 log (§4.1),
    as time lines relative to Space down, with the stroke giving the last length."""

    def check(self, events: str, want):
        toks, _ = run(shifted(1000, events))
        self.assertEqual(shape(toks)[-1], want)

    def test_sentence_6_ryo(self):
        self.check("S+0 E+20 E-103 L+124 L-207 S-228", ("AmbShift", "l", 1, 2))

    def test_sentence_17_nyu(self):
        self.check("U+-21 S+0 U-81 F+119 S-184 F-204", ("AmbShift", "f", 1, 2))

    def test_sentence_16_late_release(self):
        self.check("U+-300 U--218 F+-21 S+0 A+70 S-90 F-90 A-134", ("Plain", "a"))

    def test_sentence_17_late_release(self):
        self.check("S+0 T+20 T-115 L+135 S-155 L-206", ("Plain", "l"))

    def test_sentence_37_late_release(self):
        self.check("D+-22 S+0 D-58 L+96 S-117 L-184", ("Plain", "l"))

    def test_sentence_10_after_lone_space(self):
        self.check("G+-300 G--187 S+0 E+143 S-218 E-239", ("Plain", "e"))


def legacy_shape(tokens):
    return [(type(t).__name__, t.key) for t in tokens]


def drive(fsm, events):
    """events: (t, kind, key, down). Returns (tokens, entered S7, short-overlap fallback)."""
    out = []
    held = fallback = False
    for t, kind, key, down in events:
        if fsm.timer_deadline is not None and fsm.timer_deadline <= t:
            out += fsm.on_timeout(fsm.timer_deadline)
        before = fsm.state
        out += (fsm.on_down if down else fsm.on_up)(kind, key, t)
        held |= fsm.state == "S7"
        fallback |= (before, fsm.state) in (("S4", "S3"), ("S5", "S2"))
    if fsm.timer_deadline is not None:
        out += fsm.on_timeout(fsm.timer_deadline)
    return out, held, fallback


def random_events(rng: random.Random) -> list[tuple[int, str, str, bool]]:
    """Strokes of a few keys and Space with random, often overlapping, timing."""
    events = []
    busy = {}
    t = 0
    for _ in range(rng.randint(1, 8)):
        t += rng.choice((0, 5, 10, 19, 20, 21, 30, 45, 60, 79, 80, 81, 120))
        kind, key = rng.choice([("O", "space")] * 2 + [("M", k) for k in "jkf"])
        start = max(t, busy.get(key, 0) + 1)
        end = start + rng.choice((10, 19, 20, 21, 40, 60, 79, 80, 81, 100, 150, 250))
        busy[key] = end
        events += [(start, kind, key, True), (end, kind, key, False)]
    events.sort(key=lambda e: (e[0], e[3]))  # ups before downs at the same time
    return events


class SameAsBeforeWithoutHeldCharacters(unittest.TestCase):
    """Strokes that never hold a character in a used press come out as before
    (S1–S6 decide as the 1252788 FSM did)."""

    def test_random_sequences(self):
        rng = random.Random(20261114)
        compared = held = fallback = 0
        for _ in range(20000):
            events = random_events(rng)
            new, s7, short = drive(ChordFsm(), events)
            old, _, _ = drive(legacy_fsm.ChordFsm(), events)
            if s7:
                held += 1
                continue
            if short:
                # fsm-v2 D-2: after a short overlap the deadline stays the
                # pending key's down + T (the old FSM re-armed it at the release).
                fallback += 1
                continue
            compared += 1
            self.assertEqual(legacy_shape(new), legacy_shape(old), events)
        self.assertGreater(compared, 10000)
        self.assertGreater(held, 1000)
        self.assertGreater(fallback, 100)

    def test_short_overlap_keeps_the_deadline(self):
        # D-2: S↓0 J↓50 S↑60 is a lone Space (t1 50 >= t2 10 < V), J pending
        # until J down + T = 130; the old FSM re-armed it at 140.
        fsm = ChordFsm()
        fsm.on_down("O", "space", 0)
        fsm.on_down("M", "j", 50)
        self.assertEqual(fsm.on_up("O", "space", 60), [ThumbTap()])
        self.assertEqual((fsm.state, fsm.timer_deadline), ("S2", 130))

    @unittest.skipUnless(STAGE1_LOG.exists(), f"no recording at {STAGE1_LOG}")
    def test_recorded_sentences(self):
        """40 sentences: with every held character read as plain, the output
        is the old one; the held ones are the six of §4.1 (five, since in
        sentence 10 Space up returns its E to S2: it follows a lone Space)."""
        traces, _ = read_log(STAGE1_LOG)
        traces = [t for t in traces if t.status == "complete"]
        self.assertEqual(len(traces), 40)
        held = []
        for trace in traces:
            new, _ = replay(trace)
            old, _ = replay(trace, legacy_fsm.ChordFsm())
            as_plain = [("Plain", o.token.key) if o.held else
                        (type(o.token).__name__, getattr(o.token, "key", o.token))
                        for o in new]
            self.assertEqual(as_plain, [(type(o.token).__name__,
                                         getattr(o.token, "key", o.token)) for o in old],
                             trace.prompt["id"])
            held += [(trace.prompt["id"], type(o.token).__name__, o.token.key)
                     for o in new if o.held]
        self.assertEqual(held, [("daily-06", "AmbShift", "l"), ("daily-16", "Plain", "a"),
                                ("daily-17", "AmbShift", "f"), ("daily-17", "Plain", "l"),
                                ("daily-37", "Plain", "l")])


if __name__ == "__main__":
    unittest.main()
