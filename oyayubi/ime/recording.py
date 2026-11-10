"""Platform-independent, append-only Stage 1 key recording (no decoder/FSM)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import TextIO

PROMPTS_PATH = Path(__file__).resolve().parents[2] / "data/stage1/prompts.json"
SCHEMA = "oyayubi.stage1.v1"
VK_NEXT_PROMPT = 0x78  # F9; Enter remains available for normal commit strokes.


@dataclass(frozen=True)
class Prompt:
    id: str
    text: str
    reading: str


def load_prompts(path: Path = PROMPTS_PATH) -> list[Prompt]:
    prompts = [Prompt(**row) for row in json.loads(path.read_text(encoding="utf-8"))]
    if not prompts or len({p.id for p in prompts}) != len(prompts):
        raise ValueError("prompts must be nonempty with unique ids")
    if any(not all(isinstance(v, str) and v for v in asdict(p).values()) for p in prompts):
        raise ValueError("prompt id/text/reading must be nonempty strings")
    return prompts


class Recorder:
    """Write each row immediately; keep the current prompt's raw events in memory.

    Times are integer milliseconds relative to the host's QPC origin. F9 release
    completes a prompt only after every other key has been released. No key,
    including Backspace/Escape/Enter, erases data or restarts a prompt.
    """

    def __init__(self, stream: TextIO, prompts: list[Prompt], *, qpc_frequency: int,
                 qpc_origin: int):
        if not prompts or len({p.id for p in prompts}) != len(prompts):
            raise ValueError("prompts must be nonempty with unique ids")
        self.stream = stream
        self.prompts = prompts
        self.index = 0
        self.active = False
        self.focused = True
        self.closed = False
        self.down: set[int] = set()
        self.events: list[dict] = []
        self._last_ms = 0
        self._write("session", 0, schema=SCHEMA, clock="QPC", time_unit="ms",
                    qpc_frequency=qpc_frequency, qpc_origin=qpc_origin,
                    layout="NICOLA-A", keyboard="single-space-laptop",
                    next_prompt_vk=VK_NEXT_PROMPT, prompt_count=len(prompts))
        self._start(0)

    @property
    def prompt(self) -> Prompt | None:
        return self.prompts[self.index] if self.index < len(self.prompts) else None

    def _write(self, row_type: str, ms: int, **fields) -> dict:
        if type(ms) is not int or ms < self._last_ms:
            raise ValueError("ms must be a nondecreasing integer")
        self._last_ms = ms
        row = {"type": row_type, "ms": ms, **fields}
        self.stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.stream.flush()
        return row

    def _start(self, ms: int) -> None:
        if self.prompt is None:
            self.close(ms, "complete")
            return
        self.events = []
        self.down.clear()
        self.active = True
        self._write("prompt_start", ms, **asdict(self.prompt))

    def _end(self, ms: int, status: str) -> None:
        self._write("prompt_end", ms, id=self.prompt.id, status=status,
                    event_count=len(self.events), held_vks=sorted(self.down))
        self.active = False
        self.index += 1
        self.down.clear()

    def on_key(self, vk: int, down: bool, ms: int) -> None:
        if self.closed or not self.active:
            return
        if type(vk) is not int or not 0 <= vk <= 255 or type(down) is not bool:
            raise ValueError("invalid vk/down")
        was_down = vk in self.down
        row = self._write("key", ms, id=self.prompt.id, vk=vk,
                          action="down" if down else "up")
        self.events.append(row)
        if down:
            self.down.add(vk)
        else:
            self.down.discard(vk)
        if vk == VK_NEXT_PROMPT and not down and was_down and not self.down:
            self._end(ms, "complete")
            self._start(ms)

    def on_focus(self, focused: bool, ms: int) -> None:
        if self.closed or focused == self.focused:
            return
        self.focused = focused
        self._write("focus", ms, focused=focused)
        if not focused and self.active:
            self._end(ms, "interrupted")
        elif focused:
            self._start(ms)

    def close(self, ms: int, reason: str = "window_closed") -> None:
        if self.closed:
            return
        if self.active:
            self._end(ms, "incomplete")
        self._write("session_end", ms, reason=reason)
        self.closed = True
