"""The chords of one press alternate (album design 2 §3.3–§3.4)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from oyayubi.chord.fsm import AmbShift
from oyayubi.ime.bunsetsu import BOS_ID, candidates
from oyayubi.ime.learning import LearningView
from oyayubi.ime.predict import typed_readings
from oyayubi.ime.session import ImeSession
from oyayubi.lattice.alternation import Plan, restrict
from oyayubi.lattice.mozc_dict import load_lex
from oyayubi.lattice.table import load_table
from oyayubi.lattice.token_viterbi import decode_tokens, nbest_tokens, spans_from
from tests.test_bunsetsu import lexicon
from tests.test_press_tokens import type_events

TABLE = load_table()


def chord(key: str, press: int | None = None, turn: int | None = None) -> dict:
    faces = {f: TABLE["keys"][key][f] for f in ("left", "right") if TABLE["keys"][key][f]}
    token = {"kind": "amb" if len(faces) > 1 else "shift", "key": key, "faces": faces}
    if press is not None:
        token.update(press=press, turn=turn)
    return token


def plain(key: str) -> dict:
    return {"kind": "plain", "key": key, "kana": TABLE["keys"][key]["plain"]}


def readings(tokens: list[dict]) -> set[str]:
    return {r for end, r, _ in Plan(tokens).walk(tokens, 0, lambda _: True)
            if end == len(tokens)}


# U: ぢ/に, F: ゅ/げ. に(right) ゅ(left) alternates; に げ does not.
NYU = [chord("u", 1, 1), chord("f", 1, 2)]
FREE = [chord("u"), chord("f")]
LEX = lexicon([
    ("にげ", "逃げ", 1, 1, 10), ("に", "似", 1, 1, 5), ("げ", "下", 1, 1, 5),
    ("にゅ", "ニュ", 1, 1, 100), ("ぢ", "治", 1, 1, 200),
], [[0, 0], [0, 0]])


class PlanTest(unittest.TestCase):
    def test_one_press_reads_two_ways(self):
        self.assertEqual(readings(FREE), {"ぢゅ", "ぢげ", "にゅ", "にげ"})
        self.assertEqual(readings(NYU), {"にゅ", "ぢげ"})
        album = [chord(k, 1, i) for i, k in enumerate("jesdisih,", 1)]
        self.assertEqual(readings(album), {"おりじなるあるばむ", "どだあでぐじぐみぺ"})

    def test_separate_presses_do_not_bind(self):
        self.assertEqual(readings([chord("u", 1, 1), chord("f", 2, 1)]),
                         {"ぢゅ", "ぢげ", "にゅ", "にげ"})

    def test_a_plain_between_does_not_count(self):
        # A late release (plain) between two chords of one press.
        self.assertEqual(readings([chord("u", 1, 1), plain("a"), chord("f", 1, 2)]),
                         {"にうゅ", "ぢうげ"})

    def test_turns_count_faceless_chords(self):
        # Turn 2 was [ (no face): turns 1 and 3 have the same face.
        self.assertEqual(readings([chord("u", 1, 1), chord("f", 1, 3)]), {"ぢゅ", "にげ"})

    def test_one_sided_key_settles_the_direction(self):
        # ; has only the right face (っ): at turn 2 the press is left first.
        self.assertEqual(readings([chord("u", 1, 1), chord(";", 1, 2)]), {"ぢっ"})

    def test_unspellable_press_stays_free(self):
        # Q and Z have only the left face; turns 1 and 2 cannot both be left.
        tokens = [chord("q", 1, 1), chord("z", 1, 2), chord("u", 1, 3)]
        self.assertEqual(Plan(tokens).press, [None, None, None])
        self.assertEqual(readings(tokens), {"ぁぅぢ", "ぁぅに"})
        self.assertNotIn("press", restrict(tokens, 0, 3)[2])

    def test_restrict_keeps_the_whole_press_and_pins(self):
        # The slice [1, 2) alone would allow both faces of F.
        tokens = [chord("u", 1, 1), chord(";", 1, 2), chord("f", 1, 3)]
        self.assertEqual(restrict(tokens, 2, 3)[0]["faces"], {"left": "ゅ"})
        self.assertEqual(restrict(NYU, 1, 2, {1: frozenset({"left"})})[0]["faces"],
                         {"right": "げ"})

    def test_read_and_directions(self):
        plan = Plan(NYU)
        self.assertIsNone(plan.read(0, 2, "にげ"))
        self.assertEqual(plan.read(0, 1, "に").exit, "right")
        self.assertEqual(plan.read(1, 2, "ゅ").enter, "right")
        self.assertEqual(plan.directions(1, 2, "ゅ"), {1: frozenset({"right"})})
        self.assertEqual(plan.directions(0, 2, "に"), {})  # does not cover the interval


class ConversionTest(unittest.TestCase):
    """Dictionary arcs, the 1-best, the k-best, candidates, learning, prediction."""

    def test_spans_follow_the_rule(self):
        got = {(w.start, w.end, w.entry.reading) for i in range(2)
               for w in spans_from(NYU, i, LEX) if not w.unknown}
        self.assertNotIn((0, 2, "にげ"), got)
        self.assertIn((0, 2, "にゅ"), got)

    def test_decode_rejects_a_word_boundary_that_breaks_the_rule(self):
        self.assertEqual(decode_tokens(FREE, LEX)[0].surface, "逃げ")
        # 似+下 (に げ) breaks the rule across a word boundary: ニュ is left.
        self.assertEqual(decode_tokens(NYU, LEX)[0].surface, "ニュ")

    def test_nbest_only_alternating_readings(self):
        free = {d.reading for d in nbest_tokens(FREE, LEX, k=20)[0]}
        self.assertIn("にげ", free)
        bound = {d.reading for d in nbest_tokens(NYU, LEX, k=20)[0]}
        self.assertEqual(bound, {"にゅ", "ぢげ"})

    def test_candidates_only_alternating_readings(self):
        rows = list(candidates(NYU, LEX, BOS_ID, BOS_ID))
        self.assertEqual({c.reading for c in rows}, {"にゅ", "ぢげ"})

    def test_learned_spans_follow_the_rule(self):
        row = SimpleNamespace(surface="学", lid=1, rid=1, cost=0)
        entries = {"にげ": [row], "にゅ": [row]}
        view = SimpleNamespace(_entries=entries,
                               has_prefix=lambda r: any(k.startswith(r) for k in entries))
        got = [e.reading for _, e in LearningView.spans(view, NYU, 0, LEX)]
        self.assertEqual(got, ["にゅ"])
        self.assertEqual(sorted(e.reading for _, e in LearningView.spans(view, FREE, 0, LEX)),
                         ["にげ", "にゅ"])

    def test_predicted_readings_follow_the_rule(self):
        self.assertEqual(set(typed_readings(NYU, lambda _: True, 1)), {"にゅ", "ぢげ"})


class RealDictionaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lex = load_lex()

    def session(self) -> ImeSession:
        s = ImeSession(_lex=self.lex, learning_path=None)
        s.load()
        return s

    def test_nyuuryoku_as_typed(self):
        # The 11/11 recording, sentence 17: にゅ fused in one press (§2.1).
        s = self.session()
        type_events(s, "u+1000 space+1021 u-1102 f+1140 space-1205 f-1225 a+1225 a-1251 "
                       "space+1464 e+1485 e-1569 space-1589 l+1609 space+1629 space-1669 "
                       "l-1689 i+1848 i-1944")
        self.assertEqual(s.composition, "入力")

    def test_original_album_in_one_press_has_two_readings(self):
        s = self.session()
        s.tokens = [AmbShift(k, 1, i) for i, k in enumerate("jesdisih,", 1)]
        s._refresh()
        lattice = s._lattice()
        self.assertEqual({d.reading for d in nbest_tokens(lattice, self.lex, k=50)[0]},
                         {"おりじなるあるばむ"})
        self.assertEqual({c.reading for c in candidates(lattice, self.lex, BOS_ID, BOS_ID)},
                         {"おりじなるあるばむ", "どだあでぐじぐみぺ"})

    def test_a_clause_boundary_inside_a_press_keeps_one_direction(self):
        s = self.session()
        s.tokens = [AmbShift(k, 1, i) for i, k in enumerate("jesdisih,", 1)]
        s._refresh()
        s.converted = True
        s._open_cands()
        s._resize(-1)  # おりじなるあるば | む: the boundary is inside the press
        self.assertEqual([(g.start, g.end) for g in s.segments], [(0, 8), (8, 9)])
        self.assertEqual(s.segments[1].selected.reading, "む")
        s.segments[0].choices.last()
        self.assertEqual({c.reading for c in s.segments[0].choices.cache},
                         {"おりじなるあるば"})
        s.focus = 1
        s._open_cands()
        s.segments[1].choices.last()
        self.assertEqual({c.reading for c in s.segments[1].choices.cache}, {"む"})


if __name__ == "__main__":
    unittest.main()
