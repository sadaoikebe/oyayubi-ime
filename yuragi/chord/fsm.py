"""Host-side NICOLA chord state machine (S1–S7).

States are the thumb (Space) state and whether a character is pending:

| state | Space | pending character |
| --- | --- | --- |
| S1 | up | none (idle) |
| S2 | up | yes (character first) |
| S3 | down, unused | none (thumb first) |
| S4 | down, unused | yes, the character came first |
| S5 | down, unused | yes, the thumb came first |
| S6 | down, used | none: the press has done its job (a chord, or a lone Space) |
| S7 | down, used | yes: a character typed while that press is still held |

A press is one Space down..up. Its first use is a chord (the character with
the thumb) or a lone Space (ThumbTap, after the 80 ms window). Each press gets
a serial number; every AmbShift carries it and its position among the press's
AmbShifts (1, 2, ...), so the conversion can make the thumbs of one press
alternate. Plain and ThumbTap do not count.

S1–S6 decide as before: the 80 ms window (T) and the 20 ms overlap (V). The
deadline is the pending key's down + T and is not re-armed when S4/S5 fall
back after a short overlap.

S7 (a character m held inside a used press) decides at its exit, comparing
the overlap with Space ("cover": m down to the exit) with what would still be
left of m after it, estimated from the last released character's held length
("last"): a chord if cover >= last - cover, else a plain character. No last
length yet (first stroke): T stands in. Exits:

- Space up: cover < V leaves m pending (S2); else the comparison, then S1.
- m up: m lay entirely inside the press, a chord; S6.
- deadline (m down + T): a chord, as S4/S5; S6.
- next character m': the comparison with the cover so far; m' pending in S7.

After a lone Space (press job "tap") a character held in the press is plain:
its exits give a plain character, and Space up leaves it pending (S2).

Everything is decided once, from key order and timing; a token is never
changed after it is emitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TIMEOUT_MS = 80
OVERLAP_MS = 20

State = Literal["S1", "S2", "S3", "S4", "S5", "S6", "S7"]


@dataclass(frozen=True, slots=True)
class Plain:
    key: str


@dataclass(frozen=True, slots=True)
class AmbShift:
    """A chord: the key with one thumb, left or right unknown.

    press/turn say where it was typed (the press serial number, and 1, 2, ...
    among that press's chords), not what it is, so they do not take part in
    equality. The state machine always sets them; a chord made without them
    (a test, an expected reading) belongs to no press.
    """
    key: str
    press: int | None = field(default=None, compare=False)
    turn: int | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class ThumbTap:
    key: str = "space"


Token = Plain | AmbShift | ThumbTap


@dataclass
class ChordFsm:
    timeout_ms: int = TIMEOUT_MS
    overlap_ms: int = OVERLAP_MS
    state: State = "S1"
    m_key: str | None = None  # the pending character
    m_time: int = 0
    o_time: int = 0  # Space down of the current press
    press: int = 0  # serial number of the current (or last) press
    turn: int = 0  # AmbShifts emitted in the current press
    job: Literal["chord", "tap"] | None = None  # how the press was first used
    timer_deadline: int | None = None
    last_length: int | None = None  # held length of the last released character
    down_keys: set[str] = field(default_factory=set)
    pressed_at: dict[str, int] = field(default_factory=dict)  # character key → down time

    def reset(self) -> None:
        """Close the press logically (literal/edit boundary). Physical keys,
        the press counter and the last held length are facts that stay."""
        self.state = "S1"
        self.m_key = None
        self.job = None
        self.timer_deadline = None

    # -- tokens ----------------------------------------------------------

    def _plain(self) -> list[Token]:
        assert self.m_key is not None
        token = Plain(self.m_key)
        self.m_key = None
        return [token]

    def _shift(self) -> list[Token]:
        assert self.m_key is not None
        self.turn += 1
        token = AmbShift(self.m_key, self.press, self.turn)
        self.m_key = None
        if self.job is None:
            self.job = "chord"
        return [token]

    def _follower(self, now: int) -> list[Token]:
        """S7's m at Space up or at the next character: the cover comparison."""
        if self.job == "tap":
            return self._plain()
        cover = now - self.m_time
        last = self.last_length if self.last_length is not None else self.timeout_ms
        return self._shift() if cover >= last - cover else self._plain()

    def _held_follower(self) -> list[Token]:
        """S7's m at its own release or at the deadline: a chord."""
        if self.job == "tap":
            return self._plain()
        return self._shift()

    # -- events ----------------------------------------------------------

    def on_down(self, kind: str, key: str, now: int) -> list[Token]:
        kid = f"{kind}:{key}"
        if kid in self.down_keys:
            return []  # HID repeat
        self.down_keys.add(kid)
        if kind == "M":
            self.pressed_at[key] = now
            return self._m_down(key, now)
        if kind == "O":
            self._o_down(now)
        return []

    def on_up(self, kind: str, key: str, now: int) -> list[Token]:
        self.down_keys.discard(f"{kind}:{key}")
        if kind == "M":
            out = self._m_up(now) if key == self.m_key else []
            start = self.pressed_at.pop(key, None)
            if start is not None:
                self.last_length = now - start
            return out
        if kind == "O":
            return self._o_up(now)
        return []

    def on_timeout(self, now: int) -> list[Token]:
        if self.timer_deadline is None or now < self.timer_deadline:
            return []
        self.timer_deadline = None
        st = self.state
        if st == "S2":
            self.state = "S1"
            return self._plain()
        if st == "S3":
            self.state = "S6"
            self.job = "tap"
            return [ThumbTap()]
        if st in ("S4", "S5"):
            self.state = "S6"
            return self._shift()
        if st == "S7":
            self.state = "S6"
            return self._held_follower()
        return []

    def _m_down(self, key: str, now: int) -> list[Token]:
        st = self.state
        out: list[Token] = []
        if st == "S1":
            self.state = "S2"
        elif st == "S2":
            out = self._plain()
        elif st == "S3":
            self.state = "S5"
        elif st == "S4":
            t1 = self.o_time - self.m_time
            t2 = now - self.o_time
            if t1 < t2:
                out = self._shift()
                self.state = "S7"
            else:
                out = self._plain()
                self.state = "S5"
        elif st == "S5":
            out = self._shift()
            self.state = "S7"
        elif st == "S6":
            self.state = "S7"
        elif st == "S7":
            out = self._follower(now)
        self.m_key = key
        self.m_time = now
        self.timer_deadline = now + self.timeout_ms
        return out

    def _o_down(self, now: int) -> None:
        # One thumb key: Space cannot go down again inside a press.
        self.state = {"S1": "S3", "S2": "S4"}[self.state]
        self.press += 1
        self.turn = 0
        self.job = None
        self.o_time = now
        self.timer_deadline = now + self.timeout_ms

    def _m_up(self, now: int) -> list[Token]:
        st = self.state
        if st == "S2":
            self.state = "S1"
            self.timer_deadline = None
            return self._plain()
        if st == "S4":
            t1 = self.o_time - self.m_time
            t2 = now - self.o_time
            if t1 >= t2 and t2 < self.overlap_ms:
                self.state = "S3"
                self.timer_deadline = self.o_time + self.timeout_ms
                return self._plain()
            self.state = "S6"
            self.timer_deadline = None
            return self._shift()
        if st == "S5":
            self.state = "S6"
            self.timer_deadline = None
            return self._shift()
        if st == "S7":
            self.state = "S6"
            self.timer_deadline = None
            return self._held_follower()
        return []

    def _o_up(self, now: int) -> list[Token]:
        st = self.state
        if st == "S3":
            self.state = "S1"
            self.timer_deadline = None
            return [ThumbTap()]
        if st == "S4":
            self.state = "S1"
            self.timer_deadline = None
            return self._shift()
        if st == "S5":
            t1 = self.m_time - self.o_time
            t2 = now - self.m_time
            if t1 >= t2 and t2 < self.overlap_ms:
                self.state = "S2"
                self.timer_deadline = self.m_time + self.timeout_ms
                return [ThumbTap()]
            self.state = "S1"
            self.timer_deadline = None
            return self._shift()
        if st == "S6":
            self.state = "S1"
            return []
        if st == "S7":
            if self.job == "tap" or now - self.m_time < self.overlap_ms:
                self.state = "S2"  # m stays pending; its deadline m down + T stands
                return []
            out = self._follower(now)
            self.state = "S1"
            self.timer_deadline = None
            return out
        return []
