"""IME session: chord FSM + Mozc lattice decode + composition.

Literal characters split the lattice into independent BOS/EOS-delimited runs.
The FSM's timing and chord decisions are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
import unicodedata

from oyayubi.chord.fsm import AmbShift, ChordFsm, Plain, ThumbTap, Token
from oyayubi.chord.keys import (
    SHIFT_VKS, SHORTCUT_VKS, VK_BACK, VK_ESCAPE, VK_RETURN, classify, literal_character,
)
from oyayubi.lattice.expand import is_literal_face, literal_text
from oyayubi.lattice.mozc_dict import MozcLex, left_id_after, load_lex
from oyayubi.lattice.table import load_table
from oyayubi.lattice.token_viterbi import decode_tokens
from .bunsetsu import (BOS_ID, PAGE_SIZE, Candidate, CandidateStream, Segment,
                       candidates, segments_from)
from .learning import DEFAULT_LEARNING_PATH, LearningStore
from .lm_rescore import CONTEXT_CHARS, LmRescorer, conversion_picks
from .numbers import is_digit, number_spellings
from .personal import PersonalLexicon
from .predict import DEFAULT_MODE as DEFAULT_PREDICTION, MAX_TAILS, ReadingIndex, predict

VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_PRIOR = 0x21
VK_NEXT = 0x22
VK_UP = 0x26
VK_DOWN = 0x28
VK_F6 = 0x75
VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_F10 = 0x79
SCRIPT_VKS = (VK_F6, VK_F7, VK_F8, VK_F9, VK_F10)  # the one-shot spellings
VK_TAB = 0x09
PREDICTION = "predict"  # Segment.context of the predictions' segment


@dataclass(frozen=True, slots=True)
class Literal:
    text: str

    def __post_init__(self) -> None:
        if len(self.text) != 1:
            raise ValueError("a literal token must contain exactly one character")


_HALF_KATAKANA: dict[str, str] = {}
for _code in range(0xFF66, 0xFF9E):
    _half = chr(_code)
    _HALF_KATAKANA.setdefault(unicodedata.normalize("NFKC", _half), _half)
    for _mark in ("\uff9e", "\uff9f"):
        _full = unicodedata.normalize("NFKC", _half + _mark)
        if len(_full) == 1:
            _HALF_KATAKANA.setdefault(_full, _half + _mark)


def half_katakana(hiragana: str) -> str:
    """F8: ひらがな → ﾊﾝｶｸｶﾀｶﾅ (a kana with no half-width form stays full-width)."""
    out = []
    for ch in hiragana:
        kata = chr(ord(ch) + 0x60) if "ぁ" <= ch <= "ゖ" else ch
        out.append(_HALF_KATAKANA.get(kata, kata))
    return "".join(out)


def alnum_spelling(keys: str, current: str | None, *, full: bool) -> str:
    """F9/F10: the typed keys; again on the same width cycles the letter case."""
    forms = [keys.lower(), keys.upper(), keys[:1].upper() + keys[1:].lower()]
    if full:
        forms = [to_full_width(f) for f in forms]
    if current in forms:
        return forms[(forms.index(current) + 1) % len(forms)]
    return forms[0]


def to_full_width(text: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in text)


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
        # Where the chord was typed: the conversion alternates the thumbs of one press.
        return {"kind": kind, "key": tok.key, "faces": faces,
                "press": tok.press, "turn": tok.turn}
    return None


@dataclass(frozen=True)
class ConversionRequest:
    """Everything the LM worker needs for one conversion, captured at ThumbTap."""
    parts: list  # ImeSession._parts()
    lm_context: str
    left_surface: str
    learning: object | None  # LearningStore.snapshot()
    key: tuple  # (repr(parts), lm_context, left_surface, learning revision or None)


@dataclass
class ImeSession:
    fsm: ChordFsm = field(default_factory=ChordFsm)
    direct: bool = False
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
    _learning_undo: list[list[str]] = field(default_factory=list)
    # Stage 5b, off unless a rescorer is given (server --lm-rescore).
    lm: LmRescorer | None = None
    # Resident pipeline only: a ThumbTap leaves the LM pick to a worker.
    lm_deferred: bool = False
    _await: ConversionRequest | None = None  # conversion waiting for the worker
    _continuation: Callable[[], str | None] | None = None  # rest of that key event
    _left_surface: str = ""
    _lm_context: str = ""  # text committed on this connection since the last reset
    _table: dict = field(default_factory=dict)
    _lex: MozcLex | None = None
    _modifiers: set[int] = field(default_factory=set)
    _literal_down: set[int] = field(default_factory=set)
    clock: Callable[[], datetime] = datetime.now  # date rows (U3); fixed in transcripts
    personal: PersonalLexicon | None = None  # the user's word list (D1), shared
    prediction: str = DEFAULT_PREDICTION  # "off" or "tab" (predict.py)
    predict_index: ReadingIndex = field(default_factory=ReadingIndex)  # shared
    predicting: bool = False  # the focused candidates are predictions
    _word_starts: set[int] = field(default_factory=set)  # 1-best word boundaries

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
        self._lm_context = ""
        self.composition = ""
        self.segments.clear()
        self.focus = -1
        self.converted = False
        self.predicting = False
        self._clear_cands()
        self._await = None
        self._continuation = None

    def set_direct(self, direct: bool) -> None:
        """Change input mode, discarding unfinished input on a transition."""
        if not isinstance(direct, bool):
            raise ValueError("direct must be a boolean")
        if direct != self.direct:
            self.reset()
            self.direct = direct

    def on_key(
        self, down: bool, vk: int, now_ms: int, mods: list[str] | None = None,
    ) -> str | None:
        """Return the text to insert for this event (never cumulative text).

        mods is an optional snapshot: ["shift", "ctrl", "alt", "win"]. Without
        it, modifier down/up events provide the state (also used by the host).
        """
        if self.direct:
            return None
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
            if down and not self.tokens:
                self._lm_context = ""  # Ctrl/Alt/Win may move the caret or edit text.
            return None
        if down and (vk in (VK_F6, VK_F7) or (vk in SCRIPT_VKS and not shift)):
            pending = self._finish_pending()
            if shift:
                # The learning corrections live on Shift+F6/F7; the plain
                # keys carry the conventional script conversions below.
                def learning_key() -> None:
                    if vk == VK_F6:
                        if self.converted and self.candidates:
                            self.forget_candidate()
                    else:
                        self.undo_learning()
                    self._literal_down.add(vk)
                    return None
                return self._then(pending, learning_key)

            def script_key() -> None:
                self._select_script(vk)
                self._literal_down.add(vk)
                return None
            return self._then(pending, script_key)
        if kind == "EDIT":
            if not down:
                return None
            pending = self._finish_pending()
            return self._then(pending, lambda: self._edit(vk))
        if (down and shift and self.converted and self.focus >= 0
                and vk in (VK_LEFT, VK_RIGHT)):
            pending = self._finish_pending()
            self._resize(1 if vk == VK_RIGHT else -1)
            self._literal_down.add(vk)
            return pending
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
        if (down and vk == VK_TAB and not shift and self.prediction != "off"
                and self._predict_key()):
            self._literal_down.add(vk)
            return None
        literal = literal_character(vk, shift)
        if literal is not None:
            if not down:
                return None
            pending = self._finish_pending()

            def literal_key() -> str | None:
                self._literal_down.add(vk)
                return self._ingest([Literal(literal)])
            return self._then(pending, literal_key)
        if kind not in ("M", "O"):
            if down and not self.tokens:
                self._lm_context = ""  # Arrows/Home/Tab etc.: the caret may have moved.
            return None
        assert key is not None
        due = self.flush_timeout(now_ms)

        def chord_key() -> str | None:
            if down and f"{kind}:{key}" in self.fsm.down_keys:
                return None  # HID repeat is not the next character stroke.
            insert = ""
            if down and kind == "M" and self.converted:
                # Preserve a pending Space chord: only composition is committed here.
                insert += self._commit(reset_input=False)
            raw = (self.fsm.on_down(kind, key, now_ms) if down
                   else self.fsm.on_up(kind, key, now_ms))
            return insert + (self._ingest(raw) or "") or None
        return self._then(due, chord_key)

    def _then(self, text: str | None, rest: Callable[[], str | None]) -> str | None:
        """Run the rest of a key event now, or after the awaited conversion resolves."""
        if self._await is not None:
            self._continuation = rest
            return text
        return ((text or "") + (rest() or "")) or None

    def conversion_request(self) -> ConversionRequest | None:
        """The LM work for converting now; None without a kana run."""
        parts = self._parts()
        if not any(isinstance(part, list) for part in parts):
            return None
        self._sync_personal()
        learning = self._learning.snapshot() if self._learning is not None else None
        return ConversionRequest(
            parts, self._lm_context, self._left_surface, learning,
            (repr(parts), self._lm_context, self._left_surface,
             learning.revision if learning is not None else None,
             self.personal.revision if self.personal is not None else None))

    def finish_conversion(self, picks) -> str:
        """Resolve the awaited conversion (picks None: the Viterbi 1-best).

        Returns the text committed by the rest of the deferred key event.
        """
        assert self._await is not None
        self._await = None
        self.converted = True
        if picks is not None:
            self._refresh(picks=picks)  # rebuild the segments from the LM's picks
        self._open_cands()
        rest, self._continuation = self._continuation, None
        text = (rest() if rest is not None else None) or ""
        assert self._await is None
        return text

    def on_timeout(self, now_ms: int) -> str | None:
        if self.direct:
            return None
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

    def _predict_key(self) -> bool:
        """Tab: open predictions, or the next one; False leaves Tab as it was.

        Only unconverted input with no chord or conversion in flight asks;
        with no prediction nothing changes (Tab keeps its old meaning).
        """
        if self.predicting:
            self._cycle(1)
            return True
        if (not self.tokens or self.converted or self._await is not None
                or self.fsm.timer_deadline is not None or self._lex is None):
            return False
        self._sync_personal()
        learning = self._learning.snapshot() if self._learning is not None else None
        lattice = self._lattice()
        run = len(lattice)  # the trailing kana run: no literal inside a prediction
        while run > 0 and lattice[run - 1]["kind"] != "literal":
            run -= 1
        starts = sorted(b for b in self._word_starts if run <= b < len(lattice))
        for start in starts[-MAX_TAILS:]:  # longest tail first
            index = next(i for i, s in enumerate(self.segments) if s.end > start)
            head = self.segments[:index]
            redecoded = self.segments[index].start != start
            if redecoded:
                # Inside a clause (いい|天気): the typed head before it, decoded alone.
                tokens = self.tokens
                # Lattice offsets skip tokens with no kana; cut the raw list there.
                kept = [i for i, t in enumerate(tokens)
                        if to_lattice_token(t, self._table) is not None]
                self.tokens = tokens[:kept[start]]
                self._refresh()
                head = self.segments
                self.tokens = tokens
            left = head[-1].selected.surface if head else self._left_surface
            found = predict(lattice[start:], self._lex, self.predict_index, learning, left,
                            self.personal.revision if self.personal is not None else None)
            if not found:
                if redecoded:
                    self._refresh()  # restore the clauses exactly as shown
                continue
            stream = CandidateStream(iter(found))
            stream.ensure(0)
            self.segments = head + [Segment(start, len(lattice), found[0], fixed=True,
                                            choices=stream, context=PREDICTION)]
            self.converted = True
            self.predicting = True
            self.focus = len(head)
            self.composition = "".join(s.selected.surface for s in self.segments)
            self._open_cands()
            return True
        return False

    def _select_script(self, vk: int) -> None:
        """F6-F10: give the focused clause one fixed spelling (MS-IME's gestures).

        F6 hiragana, F7 katakana: the kana rows that close out every clause's
        candidate stream, one hiragana/katakana pair per reading the typed
        chords allow; selecting one is an ordinary candidate pick, so a later
        commit learns it like any other selection. Pressed again while a row
        of that script is selected, F6/F7 step to the next reading's row,
        wrapping to the first: a single thumb key leaves left/right faces
        ambiguous, and the decoder's reading need not be the one meant
        (クアンバ: あ/じ and ば/み share keys, so F7 goes
        クジンバ → クジンミ → クアンミ → クアンバ).
        F8 half-width katakana of the selected kana reading (else the first).
        F9/F10 the typed keys as full-/half-width alphanumerics (the kana-input
        convention: what the keys would have typed with the IME off); pressed
        again they cycle lower → UPPER → Capitalized. Not learned.
        Input not yet converted converts first, as with the MS-IME gesture.
        """
        if not self.tokens:
            return
        if self.predicting:
            self._cancel_conversion()  # the kana spelling of what was typed
        if not self.converted:
            self.converted = True
            if self.lm is not None:
                self._refresh(rescore=True)
            self._open_cands()
        if self.focus < 0:
            return
        segment = self.segments[self.focus]
        if segment.literal or segment.choices is None:
            return
        stream = segment.choices
        rows: list[int] = []
        i = 0
        while len(rows) < 2 and stream.ensure(i):
            if stream.cache[i].kind == "kana":
                rows.append(i)
            i += 1
        if not rows:
            return
        if vk in (VK_F6, VK_F7):
            self._select(self._next_kana_row(segment, hiragana=vk == VK_F6,
                                             default=rows[1] if vk == VK_F7 and len(rows) > 1
                                             else rows[0]))
            return
        hira = (segment.selected if segment.selected.kind == "kana"
                else stream.cache[rows[0]])
        if vk == VK_F8:
            surface, kind = half_katakana(hira.reading), "kana"
        else:
            keys = [t.get("key", "") for t in self._lattice()[segment.start:segment.end]]
            surface = alnum_spelling("".join(keys), segment.selected.surface
                                     if segment.selected.kind == "alnum" else None,
                                     full=vk == VK_F9)
            kind = "alnum"
        chosen = Candidate(surface, hira.reading, hira.score, hira.lid, hira.rid, kind)
        segment.selected = chosen
        segment.fixed = True
        segment.index = stream.find(chosen)  # -1: shown with no highlighted row
        self.composition = "".join(s.selected.surface for s in self.segments)
        self._show_page()

    @staticmethod
    def _is_hiragana_row(c: Candidate) -> bool:
        return c.surface == c.reading  # the katakana row of the same reading differs

    def _next_kana_row(self, segment: Segment, *, hiragana: bool, default: int) -> int:
        """The kana row F6/F7 selects: the next one of that script after the
        current selection when one is already selected, else ``default``."""
        stream = segment.choices
        assert stream is not None
        current = segment.selected
        if (current.kind != "kana" or segment.index < 0
                or self._is_hiragana_row(current) != hiragana):
            return default

        def matches(i: int) -> bool:
            c = stream.cache[i]
            return c.kind == "kana" and self._is_hiragana_row(c) == hiragana

        # Kana rows close out the stream, so the search after the current row
        # never generates dictionary rows; wrapping restarts at the first one.
        i = segment.index + 1
        while stream.ensure(i):
            if matches(i):
                return i
            i += 1
        return default

    def forget_candidate(self) -> bool:
        """Suppress the highlighted pair, then choose the newly ranked first row."""
        if (self._learning is None or not self.converted or self.focus < 0
                or not self.candidates or self.cand_index < 0):
            return False
        segment = self.segments[self.focus]
        c = segment.selected
        if c.kind in ("literal", "number", "date") or not c.reading or is_literal_face(c.reading):
            return False
        if not self._learning.suppress(c.reading, c.surface):
            return False
        if segment.context == PREDICTION:
            # The forgotten word leaves the list; back to the input if none remain.
            self._cancel_conversion()
            self._predict_key()
            return True
        segment.fixed = False
        self._prepare_cands(self.focus, reselect=True)
        self.composition = "".join(s.selected.surface for s in self.segments)
        self._open_cands()
        return True

    def undo_learning(self) -> bool:
        """Undo this session's last persisted confirmation group; keep the text."""
        if self._learning is None or not self._learning_undo:
            return False
        if not self._learning.undo(self._learning_undo[-1]):
            return False
        self._learning_undo.pop()
        if self.converted:
            self._open_cands()  # Preserve selections and manually resized intervals.
        elif self.tokens:
            self._refresh()
        return True

    def _edit(self, vk: int) -> str | None:
        if vk == VK_BACK:
            if self.converted:
                self._cancel_conversion()
            elif self.tokens:
                self.tokens.pop()
                self._refresh()
            else:
                self._lm_context = self._lm_context[:-1]
                if self.committed:
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
            eligible = _stops(self.segments)
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
        self._prepare_cands(self.focus)
        self._show_page()

    def _prepare_cands(self, index: int, *, reselect: bool = False) -> None:
        segment = self.segments[index]
        if segment.context == PREDICTION:
            return  # a fixed list, built by _predict_key
        before = self.segments[index - 1] if index > 0 else None
        if segment.selected.kind == "number":
            if segment.choices is None:
                digits = segment.selected.reading
                segment.choices = CandidateStream(iter([
                    Candidate(text, digits, 0, BOS_ID, BOS_ID, "number")
                    for text in number_spellings(digits)]))
                segment.index = segment.choices.find(segment.selected)
            return
        left = (BOS_ID if before is None
                else left_id_after(before.selected.reading, self._lex) if before.literal
                else before.selected.rid)
        right = (self.segments[index + 1].selected.lid
                 if index + 1 < len(self.segments)
                 and not self.segments[index + 1].literal else BOS_ID)
        left_surface = (self.segments[index - 1].selected.surface
                        if index > 0 else self._left_surface)
        learning = self._learning.snapshot() if self._learning is not None else None
        context = (left, right, left_surface,
                   (learning.revision, learning.now) if learning is not None else None,
                   self.personal.revision if self.personal is not None else None)
        if segment.choices is None or segment.context != context:
            assert self._lex is not None
            segment.choices = CandidateStream(candidates(
                self._lattice()[segment.start:segment.end], self._lex, left, right,
                learning=learning, left_surface=left_surface, now=self.clock()))
            segment.context = context
            if not reselect:
                segment.index = segment.choices.find(segment.selected)
        if reselect:
            # Every nonempty token interval has at least a kana/literal path.
            segment.choices.ensure(0)
            segment.selected = segment.choices.cache[0]
            segment.index = 0

    def _resize(self, delta: int) -> None:
        """Override one automatic boundary, keeping all other intervals intact."""
        segment = self.segments[self.focus]
        if segment.literal or self.predicting:
            return  # a digit run keeps its interval; predictions span the input
        following = self.focus + 1
        if delta > 0:
            if following == len(self.segments) or self.segments[following].literal:
                return
            neighbor = self.segments[following]
            segment.end += 1
            neighbor.start += 1
            if neighbor.start == neighbor.end:
                del self.segments[following]
                changed = [self.focus]
            else:
                changed = [self.focus, following]
        else:
            if segment.end - segment.start == 1:
                return
            segment.end -= 1
            if following == len(self.segments) or self.segments[following].literal:
                self.segments.insert(following, Segment(
                    segment.end, segment.end + 1, segment.selected))
            else:
                self.segments[following].start -= 1
            changed = [self.focus, following]
        # Rebuild only the intervals whose tokens changed. Seed the right
        # interval with the previous left POS, then score the focus against it.
        for index in reversed(changed):
            resized = self.segments[index]
            resized.choices = None
            resized.fixed = False
            self._prepare_cands(index, reselect=True)
        self.composition = "".join(s.selected.surface for s in self.segments)
        self._open_cands()

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
                        request = self.conversion_request() if self.lm_deferred else None
                        if request is not None:
                            assert tok is raw[-1]  # the FSM emits at most one token per call
                            self._await = request
                            continue
                        self.converted = True
                        if self.lm is not None:
                            self._refresh(rescore=True)
                        self._open_cands()
                else:
                    insert += " "
                    self._note_commit(" ")
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

    def _refresh(self, *, rescore: bool = False, picks=None) -> None:
        """Rebuild segments from the 1-best; with rescore or picks, from the LM's picks."""
        self.segments = []
        self.predicting = False
        offset = 0
        self._sync_personal()
        learning = self._learning.snapshot() if self._learning is not None else None
        left_surface = self._left_surface
        parts = self._parts()
        if rescore and self.lm is not None and picks is None:
            self.lm.last = {}
            picks = conversion_picks(parts, self._lex, learning, self._left_surface,
                                     self._lm_context, self.lm)
        if picks is not None and len(picks) != sum(isinstance(p, list) for p in parts):
            picks = None
        kana_runs = 0
        left_id = BOS_ID
        self._word_starts = set()
        for part in parts:
            if isinstance(part, str):
                last = self.segments[-1] if self.segments else None
                if is_digit(part) and last is not None and last.selected.kind == "number":
                    digits = last.selected.reading + part
                    last.end += 1
                    last.selected = Candidate(digits, digits, 0, BOS_ID, BOS_ID, "number")
                else:
                    self.segments.append(Segment(offset, offset + 1, Candidate(
                        part, part, 0, BOS_ID, BOS_ID, "number" if is_digit(part)
                        else "literal"), literal=True))
                offset += 1
                left_surface = part
                left_id = left_id_after(part, self._lex)
            elif self._lex is not None:
                if picks is not None:
                    dec = picks[kana_runs]
                else:
                    dec, _ = decode_tokens(part, self._lex, learning=learning,
                                           left_surface=left_surface, left_id=left_id)
                kana_runs += 1
                self._word_starts.update(offset + p.start for p in dec.pieces if not p.literal)
                self.segments.extend(segments_from(dec.pieces, offset, self._lex))
                offset += len(part)
                if self.segments:
                    left_surface = self.segments[-1].selected.surface
        self.focus = next(iter(_stops(self.segments)), -1)
        self.composition = "".join(s.selected.surface for s in self.segments)

    def _sync_personal(self) -> None:
        if self.personal is not None and self._lex is not None:
            self.personal.sync(self._lex)

    def _commit(self, *, reset_input: bool = True) -> str:
        text = self.composition
        left_surface = self._left_surface
        selections = []
        for segment in self.segments:
            selections.append((segment.selected, left_surface))
            left_surface = segment.selected.surface
        if self._learning is not None and self._lex is not None:
            record_ids = self._learning.confirm(selections, self._lex)
            if record_ids:
                self._learning_undo.append(record_ids)
        if reset_input:
            context = self._lm_context
            self.reset()
            self._lm_context = context  # an Enter commit keeps the document context
        else:
            self.tokens.clear()
            self.segments.clear()
            self.focus = -1
            self.composition = ""
            self.converted = False
            self.predicting = False
            self._clear_cands()
        self._left_surface = left_surface
        self._note_commit(text)
        return text

    def _note_commit(self, text: str) -> None:
        """Remember committed text as LM left context (app-independent)."""
        self._lm_context += text
        if len(self._lm_context) > 4 * CONTEXT_CHARS:
            self._lm_context = self._lm_context[-CONTEXT_CHARS:]


def _stops(segments: list[Segment]) -> list[int]:
    """Focus stops: the kana intervals; literals are skipped. Only a composition
    without kana lets a run of typed digits take focus (its spellings)."""
    kana = [i for i, s in enumerate(segments) if not s.literal]
    return kana or [i for i, s in enumerate(segments) if s.selected.kind == "number"]
