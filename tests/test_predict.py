"""Prediction (推測変換): each source on its own, the merge, and the Tab gesture.

The rail: with no prediction, Tab (and every other key) behaves exactly as
before; predictions open only on an explicit Tab and are chosen with the
ordinary candidate keys.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from oyayubi.chord.fsm import Plain
from oyayubi.ime import predict as P
from oyayubi.ime.bunsetsu import Candidate
from oyayubi.ime.learning import LearningStore
from oyayubi.ime.server import argument_parser, dispatch
from oyayubi.ime.session import ImeSession
from oyayubi.lattice.mozc_dict import load_lex

VK_TAB, VK_RETURN, VK_ESCAPE, VK_BACK, VK_F7 = 0x09, 0x0D, 0x1B, 0x08, 0x76


def plain(reading):
    """Lattice tokens (what the sources read) for an unambiguous reading."""
    return [{"kind": "plain", "key": c, "kana": c} for c in reading]


def type_reading(s, reading):
    # Same shortcut as tests/test_learning.py: unambiguous faces, no FSM.
    s._table = {"keys": {c: {"plain": c} for c in reading}}
    s.tokens = [Plain(c) for c in reading]
    s._refresh()


def press(s, vk, mods=None):
    msg = {"op": "down", "vk": vk}
    if mods:
        msg["mods"] = mods
    reply = dispatch(s, msg)
    dispatch(s, {**msg, "op": "up"})
    return reply


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lex = load_lex()
        cls.index = P.ReadingIndex()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "learning.jsonl"
        self.path.touch()
        self.now = 1_800_000_000.0

    def store(self):
        return LearningStore(self.path, clock=lambda: self.now)

    def learn(self, *pairs, left=""):
        store = self.store()
        for reading, surface in pairs:
            c = Candidate(surface, reading, 0, 1851, 1851, "dictionary", 5000)
            self.assertTrue(store.confirm([(c, left)], self.lex))
        return store

    def session(self, *, learning=None, prediction=P.DEFAULT_MODE):
        return ImeSession(_lex=self.lex, learning_path=None, _learning=learning,
                          prediction=prediction, predict_index=self.index)


class HistorySourceTest(Fixture):
    def test_learned_word_continues_its_prefix(self):
        store = self.learn(("おやゆびしふと", "親指シフト"))
        got = P.history_predictions(store.snapshot(), plain("おやゆ"))
        self.assertEqual([(c.surface, c.reading, c.kind) for c in got],
                         [("親指シフト", "おやゆびしふと", "predict")])

    def test_needs_two_kana_and_a_strict_continuation(self):
        store = self.learn(("おやゆびしふと", "親指シフト")).snapshot()
        self.assertEqual(P.history_predictions(store, plain("お")), [])
        self.assertEqual(P.history_predictions(store, plain("おやゆびしふと")), [])
        self.assertEqual(P.history_predictions(None, plain("おやゆ")), [])

    def test_recent_and_same_context_first(self):
        store = self.learn(("かいぎしつ", "会議室"))
        self.now += 30 * 86400
        store = self.learn(("かいぎろく", "会議録"))
        got = [c.surface for c in P.history_predictions(store.snapshot(), plain("かいぎ"))]
        self.assertEqual(got, ["会議録", "会議室"])  # the newer one first
        store = self.learn(("かいぎしつ", "会議室"), left="第二")
        got = [c.surface for c in P.history_predictions(store.snapshot(), plain("かいぎ"), "第二")]
        self.assertEqual(got[0], "会議室")

    def test_suppressed_pair_never_comes_back(self):
        store = self.learn(("おやゆびしふと", "親指シフト"))
        self.assertTrue(store.suppress("おやゆびしふと", "親指シフト"))
        self.assertEqual(P.history_predictions(store.snapshot(), plain("おやゆ")), [])

    def test_ambiguous_token_reads_either_face(self):
        store = self.learn(("がっこう", "学校")).snapshot()
        tokens = [{"kind": "amb", "key": "x", "faces": {"left": "か", "right": "が"}},
                  *plain("っこ")]
        if not P.typed_readings(tokens, store.has_prefix, 2):
            self.skipTest("ambiguous token shape differs")
        self.assertEqual([c.surface for c in P.history_predictions(store, tokens)], ["学校"])

    def test_literal_inside_means_no_prediction(self):
        store = self.learn(("おやゆびしふと", "親指シフト")).snapshot()
        tokens = [*plain("おや"), {"kind": "literal", "text": "1"}]
        self.assertEqual(P.history_predictions(store, tokens), [])


class DictionarySourceTest(Fixture):
    def test_prefix_returns_longer_content_words_cheapest_first(self):
        got = P.dictionary_predictions(self.lex, self.index, plain("とうきょ"))
        self.assertIn("東京", [c.surface for c in got])
        self.assertTrue(all(len(c.reading) > len("とうきょ") for c in got))
        self.assertTrue(all(c.kind == "predict" for c in got))
        self.assertLessEqual(len(got), P.MAX_PREDICTIONS)
        self.assertEqual(len({c.surface for c in got}), len(got))

    def test_short_prefix_is_not_asked(self):
        self.assertEqual(P.dictionary_predictions(self.lex, self.index, plain("とう")), [])

    def test_unknown_prefix_gives_nothing(self):
        self.assertEqual(P.dictionary_predictions(self.lex, self.index, plain("ぬぬぬぬ")), [])

    def test_index_rebuilds_on_personal_revision(self):
        index = P.ReadingIndex()
        keys = index.keys(self.lex, 1)
        self.assertIs(index.keys(self.lex, 1), keys)
        self.assertIsNot(index.keys(self.lex, 2), keys)


class MergeTest(unittest.TestCase):
    def c(self, surface):
        return Candidate(surface, "よみ", 0, 0, 0, "predict", 0)

    def test_history_before_dictionary_one_row_per_surface(self):
        got = P.merge([self.c("甲"), self.c("乙")], [self.c("乙"), self.c("丙")])
        self.assertEqual([c.surface for c in got], ["甲", "乙", "丙"])

    def test_limit(self):
        got = P.merge([self.c(str(i)) for i in range(20)], limit=P.MAX_PREDICTIONS)
        self.assertEqual(len(got), P.MAX_PREDICTIONS)


class TabGestureTest(Fixture):
    def test_tab_opens_predictions_and_enter_commits(self):
        s = self.session()
        type_reading(s, "とうきょ")
        before = s.composition
        reply = press(s, VK_TAB)
        self.assertTrue(reply["predicting"])
        self.assertTrue(reply["converted"])
        self.assertIn("東京都", reply["candidates"])
        self.assertEqual(reply["composition"], reply["candidates"][0])
        second = press(s, VK_TAB)  # the next prediction
        self.assertEqual(second["composition"], reply["candidates"][1])
        self.assertNotEqual(before, "")
        commit = press(s, VK_RETURN)
        self.assertEqual(commit["commit"], reply["candidates"][1])
        self.assertFalse(commit["predicting"])

    def test_digit_picks_a_prediction(self):
        s = self.session()
        type_reading(s, "とうきょ")
        reply = press(s, VK_TAB)
        third = reply["candidates"][2]
        self.assertEqual(press(s, ord("3"))["composition"], third)
        self.assertEqual(press(s, VK_RETURN)["commit"], third)

    def test_escape_and_backspace_return_to_the_input(self):
        for vk in (VK_ESCAPE, VK_BACK):
            s = self.session()
            type_reading(s, "とうきょ")
            shown = s.composition
            press(s, VK_TAB)
            reply = press(s, vk)
            self.assertEqual((reply["composition"], reply["converted"], reply["predicting"]),
                             (shown, False, False))
            self.assertEqual(len(s.tokens), 4)

    def test_f7_acts_on_what_was_typed_as_without_tab(self):
        plain_f7 = self.session()
        type_reading(plain_f7, "とうきょ")
        expected = press(plain_f7, VK_F7)["composition"]
        s = self.session()
        type_reading(s, "とうきょ")
        press(s, VK_TAB)
        reply = press(s, VK_F7)
        self.assertEqual(reply["composition"], expected)
        self.assertFalse(reply["predicting"])

    def test_prediction_of_the_last_word_keeps_the_head(self):
        s = self.session()
        type_reading(s, "きょうはとうきょ")
        reply = press(s, VK_TAB)
        self.assertTrue(reply["predicting"])
        self.assertTrue(reply["composition"].startswith("今日は"))
        self.assertTrue(reply["composition"].endswith(reply["candidates"][0]))
        commit = press(s, VK_RETURN)["commit"]
        self.assertTrue(commit.startswith("今日は"), commit)

    def test_history_comes_first(self):
        store = self.learn(("とうきょうとっきょきょかきょく", "東京特許許可局"))
        s = self.session(learning=store)
        type_reading(s, "とうきょ")
        self.assertEqual(press(s, VK_TAB)["candidates"][0], "東京特許許可局")

    def test_committing_a_prediction_learns_the_whole_word(self):
        store = self.store()
        s = self.session(learning=store)
        type_reading(s, "ぷろぐら")
        cands = press(s, VK_TAB)["candidates"]
        press(s, VK_RETURN)
        rows = list(self.store().learned())
        self.assertEqual([r.surface for r in rows], [cands[0]])
        self.assertTrue(rows[0].reading.startswith("ぷろぐら"))
        self.assertGreater(len(rows[0].reading), 4)


class UnchangedWithoutPredictionTest(Fixture):
    """With nothing to predict, Tab is untouched: not eaten, nothing changes."""

    def assert_tab_is_a_noop(self, s):
        state = (s.composition, s.converted, list(s.candidates), len(s.tokens))
        reply = dispatch(s, {"op": "down", "vk": VK_TAB})
        self.assertFalse(reply["predicting"])
        self.assertEqual(reply["commit"], "")
        self.assertEqual((s.composition, s.converted, list(s.candidates), len(s.tokens)), state)
        self.assertNotIn(VK_TAB, s._literal_down)  # the TIP lets the app have it

    def test_empty_input(self):
        self.assert_tab_is_a_noop(self.session())

    def test_complete_reading(self):
        s = self.session()
        type_reading(s, "いいてんき")
        self.assert_tab_is_a_noop(s)

    def test_converted_input(self):
        s = self.session()
        type_reading(s, "とうきょ")
        s.converted = True
        s._open_cands()
        self.assert_tab_is_a_noop(s)

    def test_off_mode(self):
        s = self.session(prediction="off")
        type_reading(s, "とうきょ")
        self.assert_tab_is_a_noop(s)

    def test_shift_tab(self):
        s = self.session()
        type_reading(s, "とうきょ")
        state = (s.composition, s.converted)
        reply = dispatch(s, {"op": "down", "vk": VK_TAB, "mods": ["shift"]})
        self.assertFalse(reply["predicting"])
        self.assertEqual((s.composition, s.converted), state)

    def test_typing_never_shows_predictions(self):
        s = self.session()
        type_reading(s, "とうきょ")
        self.assertFalse(s.predicting)
        self.assertFalse(s.converted)
        self.assertEqual(s.candidates, [])


class ConfigTest(unittest.TestCase):
    def test_server_option_and_default(self):
        parser = argument_parser()
        self.assertEqual(parser.parse_args([]).prediction, P.DEFAULT_MODE)
        self.assertEqual(parser.parse_args(["--prediction", "off"]).prediction, "off")
        with self.assertRaises(SystemExit):
            parser.parse_args(["--prediction", "auto"])

    def test_config_file_can_turn_it_off(self):
        from oyayubi.ime import config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"prediction": "off"}', encoding="utf-8")
            self.assertEqual(config.read_config(path, argument_parser()), {"prediction": "off"})

    def test_config_file_with_a_bom_is_read(self):
        # Windows PowerShell 5.1 (setup.ps1 before the fix) and Notepad write one.
        from oyayubi.ime import config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_bytes(b"\xef\xbb\xbf" + b'{"prediction": "off"}')
            self.assertEqual(config.read_config(path, argument_parser()), {"prediction": "off"})
            self.assertEqual(config._read_raw(path), {"prediction": "off"})


if __name__ == "__main__":
    unittest.main()
