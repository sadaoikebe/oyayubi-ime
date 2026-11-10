"""IME session: chord FSM + Mozc lattice decode + composition.

Literal characters split the lattice into independent BOS/EOS-delimited runs.
The FSM's timing and chord decisions are unchanged.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oyayubi.chord.fsm import AmbShift, ChordFsm, Plain, ThumbTap, Token
from oyayubi.chord.keys import (
    SHIFT_VKS, SHORTCUT_VKS, VK_BACK, VK_ESCAPE, VK_RETURN, classify, literal_character,
)
from tools.wp0.mozc_dict import MozcLex, load_lex
from tools.wp0.expand import is_literal_face, literal_text
from tools.wp0.table import load_table
from tools.wp0.token_viterbi import decode_tokens
from .bunsetsu import (BOS_ID, PAGE_SIZE, Candidate, CandidateStream, Segment,
                       candidates, segments_from)
from .learning import DEFAULT_LEARNING_PATH, LearningStore

VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_PRIOR = 0x21
VK_NEXT = 0x22
VK_UP = 0x26
VK_DOWN = 0x28


@dataclass(frozen=True, slots=True)
class Literal:
    text: str

    def __post_init__(self) -> None:
        if len(self.text) != 1:
            raise ValueError("a literal token must contain exactly one character")


def to_lattice_token(tok: Token | Literal, table: dict) -> dict | None:
    if isinstance(tok, Literal):
        return {"kind": "literal", "text": tok.text}
    if isinstance(tok, Plain):
        kana = table["keys"].get(tok.key, {}).get("plain")
        if not kana:
            return None
        if is_literal_face(kana):
            # Keep the canonical P=， assignment in the data. Japanese prose
            # presentation uses 、 (Stage 2 acceptance sentence).
            return {"kind": "literal", "text": literal_text(kana)}
        return {"kind": "plain", "key": tok.key, "kana": kana}
    if isinstance(tok, AmbShift):
        faces = {}
        for face in ("left", "right"):
            k = table["keys"].get(tok.key, {}).get(face)
            if k:
                faces[face] = k
        if not faces:
            return None
        kind = "amb" if len(faces) > 1 else "shift"
        return {"kind": kind, "key": tok.key, "faces": faces}
    return None


@dataclass
class ImeSession:
    fsm: ChordFsm = field(default_factory=ChordFsm)
    tokens: list[Token | Literal] = field(default_factory=list)
    committed: str = ""  # test host's document; commit return values are appended by the host
    composition: str = ""
    converted: bool = False
    candidates: list[str] = field(default_factory=list)
    cand_index: int = 0
    segments: list[Segment] = field(default_factory=list)
    focus: int = -1
    candidate_page: int = 0
    candidate_kinds: list[str] = field(default_factory=list)
    has_next_page: bool = False
    learning_path: Path | None = DEFAULT_LEARNING_PATH
    _learning: LearningStore | None = None
    _left_surface: str = ""
    _table: dict = field(default_factory=dict)
    _lex: MozcLex | None = None
    _modifiers: set[int] = field(default_factory=set)
    _literal_down: set[int] = field(default_factory=set)

    def load(self) -> None:
        if not self._table:
            self._table = load_table()
        if self._lex is None:
            self._lex = load_lex()
        if self._learning is None and self.learning_path is not None:
            self._learning = LearningStore(self.learning_path)

    def reset(self) -> None:
        """Discard composition and all input state after focus loss/termination.

        ChordFsm.reset() intentionally preserves physical down_keys during normal
        chord processing. A session reset must also forget these physical keys.
        """
        self.tokens.clear()
        self.fsm.reset()
        self.fsm.down_keys.clear()
        self._modifiers.clear()
        self._literal_down.clear()
        self._left_surface = ""
        self.composition = ""
        self.segments.clear()
        self.focus = -1
        self.converted = False
        self._clear_cands()

    def on_key(
        self, down: bool, vk: int, now_ms: int, mods: list[str] | None = None,
    ) -> str | None:
        """Return the text to insert for this event (never cumulative text).

        mods is an optional snapshot: ["shift", "ctrl", "alt", "win"]. Without
        it, modifier down/up events provide the state (also used by the host).
        """
        kind, key = classify(vk)
        if kind == "MOD":
            if down:
                self._modifiers.add(vk)
            else:
                self._modifiers.discard(vk)
            return None
        # A literal's release must not become a kana event if Shift was released
        # first. The same guard prevents native key repeat from doubling tokens.
        if vk in self._literal_down:
            if not down:
                self._literal_down.discard(vk)
            return None
        shift = "shift" in mods if mods is not None else bool(self._modifiers & SHIFT_VKS)
        shortcut = (bool(set(mods) & {"ctrl", "alt", "win"}) if mods is not None
                    else bool(self._modifiers & SHORTCUT_VKS))
        if shortcut:
            return None
        if kind == "EDIT":
            if not down:
                return None
            pending = self._finish_pending()
            return (pending or "") + (self._edit(vk) or "") or None
        # Candidate shortcuts precede native literal digits. Consume their up
        # and HID repeat too; Shift+digits still enter native symbols.
        if down and not shift and self.converted:
            if vk in (VK_LEFT, VK_RIGHT, VK_UP, VK_DOWN, VK_PRIOR, VK_NEXT) or (
                    self.candidates and ord("1") <= vk <= ord("9")):
                pending = self._finish_pending()
            else:
                pending = None
            picked = self._on_cand_key(vk)
            if picked is not None:
                self._literal_down.add(vk)
                return (pending or "") + picked
        literal = literal_character(vk, shift)
        if literal is not None:
            if not down:
                return None
            pending = self._finish_pending()
            self._literal_down.add(vk)
            insert = self._ingest([Literal(literal)])
            return (pending or "") + (insert or "") or None
        if kind not in ("M", "O"):
            return None
        assert key is not None
        due = self.flush_timeout(now_ms) or ""
        if down and f"{kind}:{key}" in self.fsm.down_keys:
            return due or None  # HID repeat is not the next character stroke.
        if down and kind == "M" and self.converted:
            # Preserve a pending Space chord: only composition is committed here.
            due += self._commit(reset_input=False)
        raw = (self.fsm.on_down(kind, key, now_ms) if down
               else self.fsm.on_up(kind, key, now_ms))
        return due + (self._ingest(raw) or "") or None

    def on_timeout(self, now_ms: int) -> str | None:
        return self._ingest(self.fsm.on_timeout(now_ms))

    def flush_timeout(self, now_ms: int) -> str | None:
        deadline = self.fsm.timer_deadline
        if deadline is None or now_ms < deadline:
            return None
        return self.on_timeout(now_ms)

    def _finish_pending(self) -> str | None:
        """End a chord at a literal/edit boundary before applying that event.

        Such keys cannot join a NICOLA chord. Resolve the pending chord using
        the existing FSM timeout path so held keys cannot appear out of order.
        """
        deadline = self.fsm.timer_deadline
        insert = self.on_timeout(deadline) if deadline is not None else None
        self.fsm.reset()
        return insert

    def _cancel_conversion(self) -> None:
        self.converted = False
        self._clear_cands()
        self._refresh()

    def _edit(self, vk: int) -> str | None:
        if vk == VK_BACK:
            if self.converted:
                self._cancel_conversion()
            elif self.tokens:
                self.tokens.pop()
                self._refresh()
            elif self.committed:
                self.committed = self.committed[:-1]
            return None
        if vk == VK_ESCAPE:
            if self.converted:
                self._cancel_conversion()
            else:
                self.reset()
            return None
        if vk == VK_RETURN:
            return self._commit()
        return None

    def _on_cand_key(self, vk: int) -> str | None:
        if vk in (VK_LEFT, VK_RIGHT):
            eligible = [i for i, s in enumerate(self.segments) if not s.literal]
            if self.focus in eligible:
                at = eligible.index(self.focus) + (1 if vk == VK_RIGHT else -1)
                self.focus = eligible[max(0, min(at, len(eligible) - 1))]
                self._open_cands()
            return ""
        if vk in (VK_DOWN, VK_UP):
            self._cycle(1 if vk == VK_DOWN else -1)
            return ""
        if vk in (VK_NEXT, VK_PRIOR):
            self._page(1 if vk == VK_NEXT else -1)
            return ""
        if self.candidates and ord("1") <= vk <= ord("9"):
            row = vk - ord("1")
            if self.focus >= 0 and row < len(self.candidates):
                self._select(self.candidate_page * PAGE_SIZE + row)
            return ""
        return None

    def _open_cands(self) -> None:
        self._clear_cands()
        if self.focus < 0:
            return
        segment = self.segments[self.focus]
        left = (self.segments[self.focus - 1].selected.rid
                if self.focus > 0 and not self.segments[self.focus - 1].literal else BOS_ID)
        right = (self.segments[self.focus + 1].selected.lid
                 if self.focus + 1 < len(self.segments)
                 and not self.segments[self.focus + 1].literal else BOS_ID)
        left_surface = (self.segments[self.focus - 1].selected.surface
                        if self.focus > 0 else self._left_surface)
        learning = self._learning.snapshot() if self._learning is not None else None
        context = (left, right, left_surface,
                   (learning.revision, learning.now) if learning is not None else None)
        if segment.choices is None or segment.context != context:
            assert self._lex is not None
            segment.choices = CandidateStream(candidates(
                self._lattice()[segment.start:segment.end], self._lex, left, right,
                learning=learning, left_surface=left_surface))
            segment.context = context
            segment.index = segment.choices.find(segment.selected)
        self._show_page()

    def _clear_cands(self) -> None:
        self.candidates = []
        self.candidate_kinds = []
        self.candidate_page = 0
        self.has_next_page = False
        self.cand_index = 0

    def _show_page(self) -> None:
        segment = self.segments[self.focus]
        stream = segment.choices
        assert stream is not None
        self.candidate_page = max(0, segment.index) // PAGE_SIZE
        start = self.candidate_page * PAGE_SIZE
        self.has_next_page = stream.ensure(start + PAGE_SIZE)
        page = stream.cache[start:start + PAGE_SIZE]
        self.candidates = [c.surface for c in page]
        self.candidate_kinds = [c.kind for c in page]
        self.cand_index = segment.index - start

    def _select(self, index: int) -> None:
        segment = self.segments[self.focus]
        stream = segment.choices
        if stream is not None and stream.ensure(index):
            segment.index = index
            segment.selected = stream.cache[index]
            segment.fixed = True
            self.composition = "".join(s.selected.surface for s in self.segments)
            self._show_page()

    def _cycle(self, delta: int) -> None:
        if self.focus < 0:
            return
        segment = self.segments[self.focus]
        stream = segment.choices
        if stream is None:
            return
        index = segment.index + delta
        if index < 0:
            index = stream.last()
        elif not stream.ensure(index):
            index = 0
        self._select(index)

    def _page(self, delta: int) -> None:
        if self.focus < 0:
            return
        segment = self.segments[self.focus]
        page = self.candidate_page + delta
        if page < 0:
            return
        start = page * PAGE_SIZE
        if segment.choices is not None and segment.choices.ensure(start):
            self._select(start)

    def segment_snapshot(self) -> list[dict]:
        offset = 0
        out = []
        for segment in self.segments:
            text = segment.selected.surface
            out.append({"start": segment.start, "end": segment.end, "surface": text,
                        "reading": segment.selected.reading, "literal": segment.literal,
                        "fixed": segment.fixed, "display_start": offset,
                        "display_end": offset + len(text)})
            offset += len(text)
        return out

    def _ingest(self, raw: list[Token | Literal]) -> str | None:
        insert = ""
        for tok in raw:
            if isinstance(tok, ThumbTap):
                if self.tokens:
                    if self.converted:
                        self._cycle(1)
                    else:
                        self.converted = True
                        self._open_cands()
                else:
                    insert += " "
                continue
            mapped = to_lattice_token(tok, self._table)
            if mapped is None:
                continue  # Undefined face: TODO in data/nicola_a.json, never guessed.
            if self.converted:
                insert += self._commit(reset_input=False)
            self.tokens.append(Literal(mapped["text"]) if mapped["kind"] == "literal" else tok)
            self.converted = False
            self._clear_cands()
            self._refresh()
        return insert or None

    def _lattice(self) -> list[dict]:
        return [d for t in self.tokens if (d := to_lattice_token(t, self._table)) is not None]

    def _parts(self) -> list[str | list[dict]]:
        parts: list[str | list[dict]] = []
        run: list[dict] = []
        for token in self._lattice():
            if token["kind"] == "literal":
                if run:
                    parts.append(run)
                    run = []
                parts.append(token["text"])
            else:
                run.append(token)
        if run:
            parts.append(run)
        return parts

    def _refresh(self) -> None:
        self.segments = []
        offset = 0
        learning = self._learning.snapshot() if self._learning is not None else None
        left_surface = self._left_surface
        for part in self._parts():
            if isinstance(part, str):
                self.segments.append(Segment(offset, offset + 1,
                    Candidate(part, part, 0, BOS_ID, BOS_ID, "literal"), literal=True))
                offset += 1
                left_surface = part
            elif self._lex is not None:
                dec, _ = decode_tokens(part, self._lex, learning=learning,
                                       left_surface=left_surface)
                self.segments.extend(segments_from(dec.pieces, offset, self._lex))
                offset += len(part)
                if self.segments:
                    left_surface = self.segments[-1].selected.surface
        self.focus = next((i for i, s in enumerate(self.segments) if not s.literal), -1)
        self.composition = "".join(s.selected.surface for s in self.segments)

    def _commit(self, *, reset_input: bool = True) -> str:
        text = self.composition
        left_surface = self._left_surface
        selections = []
        for segment in self.segments:
            selections.append((segment.selected, left_surface))
            left_surface = segment.selected.surface
        if self._learning is not None and self._lex is not None:
            self._learning.confirm(selections, self._lex)
        if reset_input:
            self.reset()
        else:
            self.tokens.clear()
            self.segments.clear()
            self.focus = -1
            self.composition = ""
            self.converted = False
            self._clear_cands()
        self._left_surface = left_surface
        return text
