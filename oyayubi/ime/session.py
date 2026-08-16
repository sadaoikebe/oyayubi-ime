"""IME session: chord FSM + Mozc lattice decode + composition."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from oyayubi.chord.fsm import AmbShift, ChordFsm, Plain, ThumbTap, Token
from oyayubi.chord.keys import VK_BACK, VK_ESCAPE, VK_RETURN, classify
from tools.wp0.mozc_dict import MozcLex, load_lex
from tools.wp0.table import load_table
from tools.wp0.token_viterbi import decode_tokens, nbest_tokens

VK_UP = 0x26
VK_DOWN = 0x28


def to_lattice_token(tok: Token, table: dict) -> dict | None:
    if isinstance(tok, Plain):
        kana = table["keys"].get(tok.key, {}).get("plain")
        if not kana:
            return None
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
    tokens: list[Token] = field(default_factory=list)
    committed: str = ""
    composition: str = ""
    converted: bool = False
    candidates: list[str] = field(default_factory=list)
    cand_index: int = 0
    _table: dict = field(default_factory=dict)
    _lex: MozcLex | None = None

    def load(self) -> None:
        if not self._table:
            self._table = load_table()
        if self._lex is None:
            self._lex = load_lex()

    def on_key(self, down: bool, vk: int, now_ms: int) -> str | None:
        """Handle a key. Returns committed text to insert, if any."""
        kind, key = classify(vk)
        if kind == "MOD":
            return None
        if down and self.converted and self.candidates:
            picked = self._on_cand_key(vk)
            if picked is not None:
                return picked
        if kind == "EDIT":
            return self._edit(vk)
        if kind != "M" and kind != "O":
            return None
        assert key is not None
        due = self.flush_timeout(now_ms)
        if down:
            raw = self.fsm.on_down(kind, key, now_ms)
        else:
            raw = self.fsm.on_up(kind, key, now_ms)
        got = self._ingest(raw)
        if due and got:
            return due + got
        return due or got

    def on_timeout(self, now_ms: int) -> str | None:
        raw = self.fsm.on_timeout(now_ms)
        return self._ingest(raw)

    def flush_timeout(self, now_ms: int) -> str | None:
        """Emit pending timeout tokens if the 80ms window has closed."""
        deadline = self.fsm.timer_deadline
        if deadline is None or now_ms < deadline:
            return None
        return self._ingest(self.fsm.on_timeout(now_ms))

    def _edit(self, vk: int) -> str | None:
        if vk == VK_BACK:
            if self.tokens:
                self.tokens.pop()
                self.converted = False
                self._clear_cands()
                self._refresh()
                return None
            if self.committed:
                self.committed = self.committed[:-1]
            return None
        if vk == VK_ESCAPE:
            if self.converted:
                self.converted = False
                self._clear_cands()
                self._refresh()
                return None
            self.tokens.clear()
            self.fsm.reset()
            self.converted = False
            self.composition = ""
            self._clear_cands()
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
        if ord("1") <= vk <= ord("9"):
            i = vk - ord("1")
            if i < len(self.candidates):
                self.cand_index = i
                self.composition = self.candidates[i]
                return self._commit()
            return ""
        return None

    def _open_cands(self) -> None:
        lat = self._lattice()
        if not lat or self._lex is None:
            self.candidates = [self.composition] if self.composition else []
            self.cand_index = 0
            return
        results, _ = nbest_tokens(lat, self._lex, k=8)
        out: list[str] = []
        seen: set[str] = set()
        if self.composition:
            out.append(self.composition)
            seen.add(self.composition)
        for dec in results:
            if dec.surface and dec.surface not in seen:
                seen.add(dec.surface)
                out.append(dec.surface)
        self.candidates = out
        self.cand_index = 0
        if out:
            self.composition = out[0]

    def _clear_cands(self) -> None:
        self.candidates = []
        self.cand_index = 0

    def _cycle(self, delta: int) -> None:
        if not self.candidates:
            return
        self.cand_index = (self.cand_index + delta) % len(self.candidates)
        self.composition = self.candidates[self.cand_index]

    def _ingest(self, raw: list[Token]) -> str | None:
        insert: str | None = None
        for tok in raw:
            if isinstance(tok, ThumbTap):
                if self.tokens:
                    if self.converted and self.candidates:
                        self._cycle(1)
                    else:
                        self.converted = True
                        self._open_cands()
                else:
                    insert = (insert or "") + " "
                continue
            self.tokens.append(tok)
            self.converted = False
            self._clear_cands()
        if raw and not self.converted:
            self._refresh()
        return insert

    def _lattice(self) -> list[dict]:
        out = []
        for t in self.tokens:
            d = to_lattice_token(t, self._table)
            if d:
                out.append(d)
        return out

    def _refresh(self) -> None:
        lat = self._lattice()
        if not lat or self._lex is None:
            self.composition = ""
            return
        dec, _ = decode_tokens(lat, self._lex)
        self.composition = dec.surface

    def _commit(self) -> str:
        text = self.composition
        self.tokens.clear()
        self.fsm.reset()
        self.composition = ""
        self.converted = False
        self._clear_cands()
        return text
