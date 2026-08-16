"""Host-side NICOLA chord FSM.

Behavior from qmk_userspace/docs/NICOLA-SPEC.md §4 (S6_OO abolished).
- Emit each token at most once.
- 80ms タイムアウトは「同時打鍵窓が閉じた」。先に押したキーは単独確定。
  Space@0 → 80ms → J は Space 単独＋「と」であり、コードではない（規格どおり）。
- ファームウェアは HID 押しっぱなしのため timeout 後も状態を残すが、IME では確定したら S1 に戻す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TIMEOUT_MS = 80
OVERLAP_MS = 20

State = Literal["S1", "S2", "S3", "S4", "S5"]


@dataclass(frozen=True, slots=True)
class Plain:
    key: str


@dataclass(frozen=True, slots=True)
class AmbShift:
    key: str


@dataclass(frozen=True, slots=True)
class ThumbTap:
    key: str = "space"


Token = Plain | AmbShift | ThumbTap


@dataclass
class ChordFsm:
    timeout_ms: int = TIMEOUT_MS
    overlap_ms: int = OVERLAP_MS
    state: State = "S1"
    m_key: str | None = None
    m_time: int = 0
    m_emitted: bool = False
    o_key: str | None = None
    o_time: int = 0
    o_emitted: bool = False
    om_emitted: bool = False
    timer_deadline: int | None = None
    down_keys: set[str] = field(default_factory=set)

    def reset(self) -> None:
        self.state = "S1"
        self.m_key = None
        self.o_key = None
        self.m_emitted = self.o_emitted = self.om_emitted = False
        self.timer_deadline = None

    def _arm(self, now: int) -> None:
        self.timer_deadline = now + self.timeout_ms

    def _disarm(self) -> None:
        self.timer_deadline = None

    def _emit_plain(self) -> list[Token]:
        if self.m_emitted or self.m_key is None:
            return []
        self.m_emitted = True
        return [Plain(self.m_key)]

    def _emit_amb(self) -> list[Token]:
        if self.om_emitted or self.m_key is None:
            return []
        self.om_emitted = True
        self.m_emitted = True
        return [AmbShift(self.m_key)]

    def _emit_tap(self) -> list[Token]:
        if self.o_emitted:
            return []
        self.o_emitted = True
        return [ThumbTap()]

    def on_down(self, kind: str, key: str, now: int) -> list[Token]:
        kid = f"{kind}:{key}"
        if kid in self.down_keys:
            return []  # HID repeat
        self.down_keys.add(kid)
        out: list[Token] = []
        if kind == "M":
            out = self._m_down(key, now)
            self.m_key = key
            self.m_time = now
            self._arm(now)
        elif kind == "O":
            out = self._o_down(key, now)
            self.o_key = key
            self.o_time = now
            self._arm(now)
        return out

    def on_up(self, kind: str, key: str, now: int) -> list[Token]:
        self.down_keys.discard(f"{kind}:{key}")
        if kind == "M":
            return self._m_up(key, now)
        if kind == "O":
            return self._o_up(key, now)
        return []

    def on_timeout(self, now: int) -> list[Token]:
        if self.timer_deadline is None or now < self.timer_deadline:
            return []
        self.timer_deadline = None
        if self.state == "S2":
            out = self._emit_plain()
            self.state = "S1"
            return out
        if self.state == "S3":
            out = self._emit_tap()
            self.state = "S1"
            return out
        if self.state in ("S4", "S5"):
            return self._emit_amb()
        return []

    def _m_down(self, key: str, now: int) -> list[Token]:
        st = self.state
        if st == "S1":
            self.m_emitted = False
            self.om_emitted = False
            self.state = "S2"
            return []
        if st == "S2":
            out = self._emit_plain()
            self.m_emitted = False
            self.om_emitted = False
            self.state = "S2"
            return out
        if st == "S3":
            self.m_emitted = False
            self.om_emitted = False
            self.state = "S5"
            return []
        if st == "S4":
            t1 = self.o_time - self.m_time
            t2 = now - self.o_time
            if t1 < t2:
                out = self._emit_amb()
                self.m_emitted = False
                self.om_emitted = False
                self.state = "S2"
                return out
            out = self._emit_plain()
            self.m_emitted = False
            self.om_emitted = False
            self.state = "S5"
            return out
        if st == "S5":
            out = self._emit_amb()
            self.m_emitted = False
            self.om_emitted = False
            self.state = "S2"
            return out
        return []

    def _o_down(self, key: str, now: int) -> list[Token]:
        st = self.state
        if st == "S1":
            self.o_emitted = False
            self.om_emitted = False
            self.state = "S3"
            return []
        if st == "S2":
            self.o_emitted = False
            self.om_emitted = False
            self.state = "S4"
            return []
        if st == "S3":
            out = self._emit_tap()
            self.o_emitted = False
            self.om_emitted = False
            self.state = "S3"
            return out
        if st == "S4":
            out = self._emit_amb()
            self.o_emitted = False
            self.om_emitted = False
            self.state = "S3"
            return out
        if st == "S5":
            t1 = self.m_time - self.o_time
            t2 = now - self.m_time
            if t1 < t2:
                out = self._emit_amb()
                self.o_emitted = False
                self.om_emitted = False
                self.state = "S3"
                return out
            out = self._emit_tap()
            self.o_emitted = False
            self.om_emitted = False
            self.state = "S4"
            return out
        return []

    def _m_up(self, key: str, now: int) -> list[Token]:
        if self.m_key != key:
            return []
        st = self.state
        out: list[Token] = []
        if st == "S2":
            out = self._emit_plain()
            self.reset()
            return out
        if st == "S4":
            t1 = self.o_time - self.m_time
            t2 = now - self.o_time
            if t1 >= t2 and t2 < self.overlap_ms:
                out = self._emit_plain()
                self.m_key = None
                self.m_emitted = False
                self.om_emitted = False
                self.state = "S3"
                self._arm(now)
                return out
            out = self._emit_amb()
            self.reset()
            return out
        if st == "S5":
            out = self._emit_amb()
            self.reset()
            return out
        return []

    def _o_up(self, key: str, now: int) -> list[Token]:
        if self.o_key != key:
            return []
        st = self.state
        if st == "S3":
            out = self._emit_tap()
            self.reset()
            return out
        if st == "S4":
            out = self._emit_amb()
            self.reset()
            return out
        if st == "S5":
            t1 = self.m_time - self.o_time
            t2 = now - self.m_time
            if t1 >= t2 and t2 < self.overlap_ms:
                out = self._emit_tap()
                self.o_key = None
                self.o_emitted = False
                self.om_emitted = False
                self.state = "S2"
                self._arm(now)
                return out
            out = self._emit_amb()
            self.reset()
            return out
        return []
