"""The thumbs of one press alternate (album design 2 §3.3–§3.4).

A chord token may carry "press" (the press serial number) and "turn" (1, 2, ...
among that press's chords; the state machine also counts chords whose key has
no face). Within one press the faces alternate, so the conversion chooses one
thing per press: its direction, the face of turn 1 ("left" or "right").

- A key with one face only (; Q Z) settles the direction of its press.
- A press that neither direction can spell (not typed by the rule) is left as
  before: each of its chords picks its face alone.
- Tokens without "press" (evaluation readings, tests) are free, as before.

Alternation is not a penalty: a path that breaks it does not exist.

Word arcs record what they need from the path around them (``Sides``):
``enter`` is the direction a press begun before the word must already have,
``exit`` the direction of a press that goes on after the word, and ``bound``
whether the word contains a constrained chord at all. A word without one
passes the path's direction through. Presses are contiguous in a lattice
(only plain characters can come between the chords of one press), so at a
word boundary at most one press is open, and a path state is one direction
or None.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator

from .expand import is_literal_face, token_branches

SIDES = frozenset(("left", "right"))
_OTHER = {"left": "right", "right": "left"}


def face_of(direction: str, turn: int) -> str:
    """The face of a turn in a press of this direction (also its inverse)."""
    return direction if turn % 2 else _OTHER[direction]


@dataclass(frozen=True, slots=True)
class Sides:
    enter: str | None = None
    exit: str | None = None
    bound: bool = False


FREE = Sides()


def admits(sides: Sides, side: str | None) -> bool:
    """Can a word with these sides follow a path whose open press has this direction?"""
    return not sides.bound or sides.enter is None or sides.enter == side


def after(sides: Sides, side: str | None) -> str | None:
    """The path's open-press direction after the word."""
    return sides.exit if sides.bound else side


class Plan:
    """Per token: its branches (kana, direction or None) and its constrained press."""

    __slots__ = ("branches", "press", "first", "last")

    def __init__(self, tokens: list[dict]):
        groups: dict[int, list[int]] = {}
        for pos, token in enumerate(tokens):
            if "press" in token:
                groups.setdefault(token["press"], []).append(pos)
        self.branches: list[list[tuple[str, str | None]]] = [
            [(kana, None) for kana in token_branches(t)] for t in tokens]
        self.press: list[int | None] = [None] * len(tokens)
        self.first: dict[int, int] = {}
        self.last: dict[int, int] = {}
        for press, positions in groups.items():
            feasible = {d for d in SIDES if all(
                face_of(d, tokens[q]["turn"]) in tokens[q]["faces"] for q in positions)}
            if not feasible:
                continue  # neither direction spells it: each chord stays free
            for q in positions:
                turn = tokens[q]["turn"]
                self.press[q] = press
                self.branches[q] = [(kana, face_of(face, turn))
                                    for face, kana in tokens[q]["faces"].items()
                                    if face_of(face, turn) in feasible]
            self.first[press], self.last[press] = positions[0], positions[-1]

    def _sides(self, start: int, end: int, enter: str | None,
               cur: tuple[int, str] | None) -> Sides:
        if cur is None:
            return FREE
        return Sides(enter, cur[1] if self.last[cur[0]] >= end else None, True)

    def _enter(self, press: int, direction: str, start: int) -> str | None:
        return direction if self.first[press] < start else None

    def walk(self, tokens: list[dict], start: int, extend: Callable[[str], bool],
             limit: int | None = None) -> Iterator[tuple[int, str, Sides]]:
        """Readings from start, one kana per token, depth first; extend prunes.

        Yields (end, reading, sides) for each nonempty reading reached. Literal
        tokens and literal faces end a reading. Within a press, only the face
        that alternates with the previous chord is followed.
        """
        stack: list[tuple[int, str, str | None, tuple[int, str] | None]] = [
            (start, "", None, None)]
        while stack:
            pos, acc, enter, cur = stack.pop()
            if acc:
                yield pos, acc, self._sides(start, pos, enter, cur)
            if pos >= len(tokens) or (limit is not None and pos - start >= limit):
                continue
            press = self.press[pos]
            for kana, d in self.branches[pos]:
                if tokens[pos]["kind"] == "literal" or is_literal_face(kana):
                    continue
                if press is not None and cur is not None and cur[0] == press and cur[1] != d:
                    continue
                nxt = acc + kana
                if not extend(nxt):
                    continue
                if press is None:
                    stack.append((pos + 1, nxt, enter, cur))
                elif cur is None:
                    stack.append((pos + 1, nxt, self._enter(press, d, start), (press, d)))
                else:
                    stack.append((pos + 1, nxt, enter, (press, d)))

    def single(self, pos: int) -> list[tuple[str, Sides]]:
        """The one-token readings at pos, each with its sides."""
        press = self.press[pos]
        if press is None:
            return [(kana, FREE) for kana, _ in self.branches[pos]]
        return [(kana, Sides(self._enter(press, d, pos),
                             d if self.last[press] > pos else None, True))
                for kana, d in self.branches[pos]]

    def read(self, start: int, end: int, reading: str) -> Sides | None:
        """The sides of a given reading over [start, end); None if it breaks the rule."""
        enter = cur = None
        for k, pos in enumerate(range(start, end)):
            press = self.press[pos]
            match = [d for kana, d in self.branches[pos] if kana == reading[k]]
            if not match:
                return None
            if press is None:
                continue
            if cur is not None and cur[0] == press:
                if cur[1] not in match:
                    return None
                continue
            d = match[0]  # the faces of a key differ, so one direction at most
            if cur is None:
                enter = self._enter(press, d, start)
            cur = (press, d)
        return self._sides(start, end, enter, cur)

    def directions(self, start: int, end: int, reading: str) -> dict[int, frozenset[str]]:
        """The directions a reading of [start, end) gives each constrained press in it.

        A reading that does not cover the interval token by token (a stale
        selection while intervals change) gives none.
        """
        if len(reading) != end - start:
            return {}
        out: dict[int, frozenset[str]] = {}
        for k, pos in enumerate(range(start, end)):
            press = self.press[pos]
            if press is not None:
                dirs = frozenset(d for kana, d in self.branches[pos] if kana == reading[k])
                out[press] = out.get(press, SIDES) & dirs
        return out


def restrict(tokens: list[dict], start: int, end: int,
             pins: dict[int, frozenset[str]] | None = None) -> list[dict]:
    """tokens[start:end] decoded on its own, keeping what the whole list decides.

    A press found unspellable over all its chords stays free (press dropped);
    a constrained chord keeps only the faces of the directions still possible,
    over the whole press and as pinned by the readings chosen around the slice.
    """
    plan = Plan(tokens)
    out = []
    for pos in range(start, end):
        token = tokens[pos]
        press = plan.press[pos]
        if press is None:
            if "press" in token:
                token = {k: v for k, v in token.items() if k not in ("press", "turn")}
        else:
            allowed = {d for _, d in plan.branches[pos]} & (pins or {}).get(press, SIDES)
            token = {**token, "faces": {face: kana for face, kana in token["faces"].items()
                                        if face_of(face, token["turn"]) in allowed}}
        out.append(token)
    return out
