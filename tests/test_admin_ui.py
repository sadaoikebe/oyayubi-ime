"""The management window's logic (S4), without tkinter or a display."""
from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from oyayubi.ime import admin_ui, config
from oyayubi.ime.admin_ui import AdminModel, flatten
from oyayubi.ime.bunsetsu import Candidate
from oyayubi.ime.learning import LearningStore, pause_marker
from oyayubi.ime.personal import read_words
from oyayubi.ime.server import argument_parser
from tests.test_manage import small_lex


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.config = self.dir / "config.json"
        self.learning = self.dir / "learning.jsonl"
        self.personal = self.dir / "personal.jsonl"
        self.config.write_text(json.dumps({"learning_file": str(self.learning),
                                           "personal_file": str(self.personal)}),
                               encoding="utf-8")
        self.model = AdminModel(runtime_dir=self.dir)

    def learn(self):
        self.learning.touch()
        store = LearningStore(self.learning)
        store.confirm([(Candidate("ヌエ", "ぬえ", 0, 1, 1, "kana"), "")], small_lex())
        store.confirm([(Candidate("肩", "かた", 0, 1851, 1851, word_cost=3000), "")],
                      small_lex())


class AdminModelTest(Base):
    def test_no_tkinter_needed(self):
        self.assertNotIn("tkinter", sys.modules)

    def test_files_come_from_the_config(self):
        self.assertEqual((self.model.config_file, self.model.learning_file,
                          self.model.personal_file), (self.config, self.learning, self.personal))

    def test_words(self):
        self.assertTrue(self.model.add_word(" おやゆび ", "親指").ok)
        self.assertTrue(self.model.add_word("かた", "型", "固有名詞", "1000").ok)
        self.assertEqual([(w.reading, w.surface, w.pos, w.cost) for w in self.model.words()],
                         [("おやゆび", "親指", "名詞", 2600), ("かた", "型", "固有名詞", 1000)])
        self.assertEqual(self.model.words(), read_words(self.personal))  # the CLI's file
        bad = [self.model.add_word("ABC", "x"), self.model.add_word("かた", "型", cost="x"),
               self.model.add_word("かた", "", "名詞"), self.model.add_word("かた", "型", "動詞")]
        self.assertEqual([o.ok for o in bad], [False] * 4)
        self.assertIn("hiragana", bad[0].message)
        self.assertTrue(self.model.remove_word("かた", "型").ok)
        self.assertFalse(self.model.remove_word("かた", "型").ok)
        self.assertEqual(len(self.model.words()), 1)

    def test_learning_off(self):
        self.assertEqual(self.model.learned(), [])
        self.assertEqual(self.model.learning_state(), "off (no file)")
        self.assertFalse(self.model.can_toggle_pause())
        for outcome in (self.model.toggle_pause(), self.model.forget("かた", "肩"),
                        self.model.promote("かた", "肩")):
            self.assertFalse(outcome.ok)
        self.assertFalse(self.learning.exists())  # nothing created behind the user's back

    def test_learning_list_search_and_counts(self):
        self.learn()
        rows = [self.model.learned_row(p) for p in self.model.learned()]
        self.assertEqual([r[:3] for r in rows], [("かた", "肩", 1), ("ぬえ", "ヌエ", 1)])
        self.assertEqual([r[4] for r in rows], ["", "word"])
        self.assertEqual([p.surface for p in self.model.learned(" ぬ ")], ["ヌエ"])
        self.model.add_word("おやゆび", "親指")
        self.assertEqual(self.model.learning_counts(),
                         "learning on · 2 learned pair(s), 1 learned word(s) · "
                         "1 registered word(s)")

    def test_pause_toggle(self):
        self.learn()
        self.assertEqual(self.model.pause_label(), "Pause learning")
        self.assertEqual(self.model.toggle_pause().message, "learning paused")
        self.assertTrue(pause_marker(self.learning).exists())
        self.assertEqual(self.model.pause_label(), "Resume learning")
        self.assertEqual(self.model.toggle_pause().message, "learning on")
        self.assertFalse(pause_marker(self.learning).exists())

    def test_forget_and_promote(self):
        self.learn()
        self.assertTrue(self.model.forget("かた", "肩").ok)
        self.assertFalse(self.model.forget("かた", "肩").ok)
        self.assertEqual([p.surface for p in self.model.learned()], ["ヌエ"])
        self.assertFalse(self.model.promote("ぬえ", "鵺").ok)  # never learned
        self.assertFalse(self.model.promote("ぬえ", "ヌエ", "動詞").ok)
        self.assertTrue(self.model.promote("ぬえ", "ヌエ", "固有名詞").ok)
        self.assertEqual([(w.reading, w.surface, w.pos) for w in read_words(self.personal)],
                         [("ぬえ", "ヌエ", "固有名詞")])

    def test_no_learning_setting_is_reported(self):
        self.assertTrue(self.model.set_setting("no_learning", "on").ok)
        self.assertEqual(self.model.learning_state(), "off (no_learning setting)")


class SettingsTest(Base):
    def rows(self):
        return {r.name: r for r in self.model.setting_rows()}

    def test_every_config_setting_is_listed(self):
        rows = self.rows()
        self.assertEqual(list(rows), list(config.file_settings(argument_parser())))
        self.assertEqual((rows["learning_file"].value, rows["learning_file"].source),
                         (str(self.learning), "file"))
        self.assertEqual((rows["lm_deadline_ms"].value, rows["lm_deadline_ms"].source),
                         ("400", "default"))
        self.assertTrue(rows["no_learning"].switch)
        self.assertFalse(rows["lm_k"].switch)
        self.assertEqual(rows["lm_alpha"].value, "")  # None: the server picks
        self.assertEqual(self.model.setting_choices("lm_prefetch"), ["thumb", "typing", "off"])
        self.assertEqual(self.model.setting_choices("lm_rescore"), ["true", "false"])
        self.assertEqual(self.model.setting_choices("lm_k"), [])
        self.assertIn("tip.json toggle_vk", dict(self.model.other_settings()))

    def test_edit_round_trips_through_the_server_parser(self):
        for name, text in (("lm_deadline_ms", "250"), ("lm_prefetch", "typing"),
                           ("lm_rescore", "false"), ("lm_k", "4"), ("log_text", "yes")):
            outcome = self.model.set_setting(name, text)
            self.assertTrue(outcome.ok, outcome.message)
            self.assertIn("restart", outcome.message)
        args = config.parse_args(argument_parser(), ["--runtime-dir", str(self.dir)])
        self.assertEqual((args.lm_deadline_ms, args.lm_prefetch, args.lm_rescore, args.lm_k,
                          args.log_text), (250.0, "typing", False, 4, True))
        self.assertEqual(args.learning_file, self.learning)  # other keys kept
        self.assertEqual(self.rows()["lm_k"].source, "file")

    def test_invalid_values_are_refused_and_not_written(self):
        before = self.config.read_text(encoding="utf-8")
        for name, text in (("lm_deadline_ms", "soon"), ("lm_prefetch", "always"),
                           ("no_learning", "maybe"), ("resident", "true"), ("lm_k", "2.5")):
            self.assertFalse(self.model.set_setting(name, text).ok, name)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_reset_and_empty_text_return_to_default(self):
        self.model.set_setting("lm_k", "4")
        self.assertTrue(self.model.set_setting("lm_k", "  ").ok)
        self.assertEqual(self.rows()["lm_k"].source, "default")
        self.assertIn("already", self.model.reset_setting("lm_k").message)

    def test_paths_follow_a_changed_setting(self):
        other = self.dir / "other.jsonl"
        self.assertTrue(self.model.set_setting("personal_file", str(other)).ok)
        self.model.add_word("おやゆび", "親指")
        self.assertEqual(len(read_words(other)), 1)
        self.assertEqual(read_words(self.personal), [])

    def test_unknown_keys_in_the_file_survive_an_edit(self):
        raw = json.loads(self.config.read_text(encoding="utf-8"))
        raw["future_option"] = 1
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        with redirect_stderr(io.StringIO()):  # the server's "unknown setting" note
            self.model.set_setting("lm_k", "4")
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["future_option"], 1)

    def test_absent_config_file_is_created_on_first_edit(self):
        self.config.unlink()
        model = AdminModel(runtime_dir=self.dir)
        self.assertTrue(model.set_setting("lm_k", "4").ok)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), {"lm_k": 4})

    def test_broken_config_file_is_reported_not_overwritten(self):
        self.config.write_text("[1, 2]", encoding="utf-8")
        self.assertFalse(self.model.set_setting("lm_k", "4").ok)
        self.assertFalse(self.model.reset_setting("lm_k").ok)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "[1, 2]")


class StatusTest(Base):
    def test_status_is_manage_status_flattened(self):
        self.learn()
        self.model.add_word("おやゆび", "親指")
        self.model.toggle_pause()
        rows = dict(self.model.status_rows())
        self.assertEqual(rows["learning.state"], "paused")
        self.assertEqual(rows["learning.pairs"], "2")
        self.assertEqual(rows["learning.learned_words"], "1")
        self.assertEqual(rows["personal_lexicon.words"], "1")
        self.assertEqual(rows["resident_server"], "not running")

    def test_flatten(self):
        self.assertEqual(flatten({"a": {"b": 1, "c": "x"}, "d": [1, "い"]}),
                         [("a.b", "1"), ("a.c", "x"), ("d", '[1, "い"]')])


class FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class WidgetLayerTest(Base):
    """AdminWindow against a stand-in tkinter: every builder and handler runs."""

    def window(self):
        tk = mock.MagicMock(StringVar=FakeVar)
        modules = {"tkinter": tk, "tkinter.ttk": tk.ttk, "tkinter.messagebox": tk.messagebox}
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        tk.messagebox.askyesno.return_value = True
        tk.ttk.Treeview.side_effect = lambda *a, **k: mock.MagicMock()  # one per table
        return admin_ui.AdminWindow(mock.MagicMock(), self.model)

    @staticmethod
    def select(w, tree, values):
        w._fill(tree, [("other", "row"), values])
        tree.selection.return_value = ("1",)

    def test_handlers_reach_the_files(self):
        self.learn()
        w = self.window()
        w.w_reading.set("おやゆび")
        w.w_surface.set("親指")
        w._add_word()
        self.assertEqual([x.surface for x in read_words(self.personal)], ["親指"])
        self.assertEqual(w.w_reading.get(), "")  # cleared after success
        self.select(w, w.words_tree, ("おやゆび", "親指", "名詞", "2600"))
        w._remove_word()
        self.assertEqual(read_words(self.personal), [])
        self.select(w, w.learning_tree, ("ぬえ", "ヌエ", "1", "", "word"))
        w._promote()
        self.assertEqual([x.surface for x in read_words(self.personal)], ["ヌエ"])
        w._forget()
        self.assertEqual([p.surface for p in self.model.learned()], ["肩"])
        self.select(w, w.settings_tree, ("lm_k", "", "default", ""))
        w._pick_setting()
        w.s_value.set("4")
        w._save_setting()
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["lm_k"], 4)
        self.assertIn("restart", w.message.get())
        w._reset_setting()
        self.assertNotIn("lm_k", json.loads(self.config.read_text(encoding="utf-8")))
        w._refresh_status()

    def test_selection_reads_the_rows_as_given(self):
        w = self.window()
        w._fill(w.words_tree, [("じゅう", "10", "名詞", 2600)])
        w.words_tree.selection.return_value = ("0",)
        w.words_tree.item.return_value = ("じゅう", 10, "名詞", 2600)  # what Tk hands back
        self.assertEqual(w._selected(w.words_tree), ("じゅう", "10", "名詞", 2600))
        w.words_tree.selection.return_value = ()
        self.assertIsNone(w._selected(w.words_tree))


class EntryPointTest(unittest.TestCase):
    def test_without_tkinter_it_says_so(self):
        import builtins
        real = builtins.__import__

        def no_tk(name, *args, **kwargs):
            if name == "tkinter":
                raise ImportError(name)
            return real(name, *args, **kwargs)

        builtins.__import__ = no_tk
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                self.assertEqual(admin_ui.main([]), 1)
        finally:
            builtins.__import__ = real
        self.assertIn("manage", err.getvalue())


if __name__ == "__main__":
    unittest.main()
