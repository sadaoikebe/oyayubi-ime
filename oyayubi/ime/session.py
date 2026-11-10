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
from tools.wp0.table import load_table
from tools.wp0.token_viterbi import decode_tokens, nbest_tokens

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
        if not ("ぁ" <= kana <= "ゖ" or kana == "ー"):
            # Keep the canonical P=， assignment in the data. Japanese prose
            # presentation uses 、 (Stage 2 acceptance sentence).
            return {"kind": "literal", "text": "、" if kana == "，" else kana}
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
    _table: dict = field(default_factory=dict)
    _lex: MozcLex | None = None
    _modifiers: set[int] = field(default_factory=set)
    _literal_down: set[int] = field(default_factory=set)

    def load(self) -> None:
        if not self._table:
            self._table = load_table()
        if self._lex is None:
            self._lex = load_lex()

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
        self.composition = ""
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
        # Existing whole-composition arrow controls remain available. Digits
        # are character input; numbered segment candidate selection is Stage 2B.
        if down and not shift and self.converted and self.candidates:
            picked = self._on_cand_key(vk)
            if picked is not None:
                return picked
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
        if vk == VK_DOWN:
            self._cycle(1)
            return ""
        if vk == VK_UP:
            self._cycle(-1)
            return ""
        return None

    def _open_cands(self) -> None:
        # Enumerate each kana run independently, then take the cheapest whole
        # strings. No POS connection or dictionary span crosses a literal.
        ranked = [("", 0)]
        for part in self._parts():
            if isinstance(part, str):
                choices = [(part, 0)]
            elif self._lex is not None:
                results, _ = nbest_tokens(part, self._lex, k=8)
                choices = [(dec.surface, dec.score) for dec in results]
                if not choices:
                    dec, _ = decode_tokens(part, self._lex)
                    choices = [(dec.surface, dec.score)]
            else:
                choices = [("", 0)]
            combined = sorted(((a + b, ca + cb) for a, ca in ranked for b, cb in choices),
                              key=lambda item: item[1])
            distinct = {}
            for text, cost in combined:
                distinct.setdefault(text, cost)
            ranked = list(distinct.items())[:8]
        out = [self.composition] if self.composition else []
        out.extend(text for text, _ in ranked if text and text not in out)
        self.candidates = out[:8]
        self.cand_index = 0
        if self.candidates:
            self.composition = self.candidates[0]

    def _clear_cands(self) -> None:
        self.candidates = []
        self.cand_index = 0

    def _cycle(self, delta: int) -> None:
        if self.candidates:
            self.cand_index = (self.cand_index + delta) % len(self.candidates)
            self.composition = self.candidates[self.cand_index]

    def _ingest(self, raw: list[Token | Literal]) -> str | None:
        insert = ""
        for tok in raw:
            if isinstance(tok, ThumbTap):
                if self.tokens:
                    if self.converted and self.candidates:
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
        out = []
        for part in self._parts():
            if isinstance(part, str):
                out.append(part)
            elif self._lex is not None:
                dec, _ = decode_tokens(part, self._lex)
                out.append(dec.surface)
        self.composition = "".join(out)

    def _commit(self, *, reset_input: bool = True) -> str:
        text = self.composition
        if reset_input:
            self.reset()
        else:
            self.tokens.clear()
            self.composition = ""
            self.converted = False
            self._clear_cands()
        return text
