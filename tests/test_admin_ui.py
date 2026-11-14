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

from yuragi.ime import admin_ui, broker_client, config
from yuragi.ime.admin_ui import SETTINGS, AdminModel
from yuragi.ime.bunsetsu import Candidate
from yuragi.ime.learning import LearningStore, pause_marker
from yuragi.ime.personal import read_words
from yuragi.ime.server import argument_parser
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
        # Never the broker of the machine running the tests: no broker by default.
        self.broker = {"status": None, "retry": False}
        for name in ("status", "retry"):
            patcher = mock.patch.object(broker_client, name,
                                        side_effect=lambda *a, n=name: self.broker[n])
            patcher.start()
            self.addCleanup(patcher.stop)
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

    def test_restart_engine_reports_no_broker(self):
        outcome = self.model.restart_engine()
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.message, "ブローカーが動いていません")

    def test_restart_engine_when_running(self):
        # REDESIGN §9.4: a settings restart is the broker's "retry".
        self.broker["retry"] = True
        outcome = self.model.restart_engine()
        self.assertTrue(outcome.ok)
        broker_client.retry.assert_called_once_with()

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
        self.assertEqual([o.message for o in bad], [
            "読みはひらがな（と ー）で入力してください",
            "コストは整数で入力してください（「x」は使えません）",
            "単語を入力してください（前後に空白は入れられません）",
            "品詞「動詞」は使えません。次から選んでください: "
            "名詞、サ変名詞、形容動詞、固有名詞、人名、名、姓、地名、組織、副詞"])
        self.assertTrue(self.model.remove_word("かた", "型").ok)
        self.assertEqual(self.model.remove_word("かた", "型").message,
                         "登録されていません: かた → 型")
        self.assertEqual(len(self.model.words()), 1)

    def test_learning_off(self):
        # REDESIGN §6.7: on/paused/off is the learning setting, not the file.
        self.assertEqual(self.model.learned(), [])
        self.assertEqual(self.model.learning_state(), "on")  # no file yet: the engine makes it
        self.assertTrue(self.model.set_setting("learning", "使わない").ok)
        self.assertEqual(self.model.learning_state(), "off")
        self.assertFalse(self.model.can_toggle_pause())
        for outcome in (self.model.toggle_pause(), self.model.forget("かた", "肩"),
                        self.model.promote("かた", "肩")):
            self.assertFalse(outcome.ok)
        self.assertFalse(self.learning.exists())  # nothing created behind the user's back

    def test_learning_list_search_and_counts(self):
        self.learn()
        rows = [self.model.learned_row(p) for p in self.model.learned()]
        self.assertEqual([r[:3] for r in rows], [("かた", "肩", 1), ("ぬえ", "ヌエ", 1)])
        self.assertEqual([r[4] for r in rows], ["", "○"])
        self.assertEqual([p.surface for p in self.model.learned(" ぬ ")], ["ヌエ"])
        self.model.add_word("おやゆび", "親指")
        self.assertEqual(self.model.learning_counts(),
                         "学習: 学習中 ・ 学習した変換 2 件（うち単語として覚えたもの 1 件） ・ "
                         "登録した単語 1 件")

    def test_pause_toggle(self):
        self.learn()
        self.assertEqual(self.model.pause_label(), "学習を一時停止")
        self.assertEqual(self.model.toggle_pause().message,
                         "学習を一時停止しました。" + admin_ui.RESTART_NOTE)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["learning"], "paused")
        self.assertEqual(self.model.pause_label(), "学習を再開")
        self.assertTrue(self.model.toggle_pause().message.startswith("学習を再開しました。"))
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["learning"], "on")

    def test_old_pause_marker_reads_as_paused_until_the_setting_is_written(self):
        self.learn()
        pause_marker(self.learning).touch()
        self.model.reload()
        self.assertEqual(self.model.learning_state(), "paused")
        self.model.toggle_pause()  # resume: the setting replaces the marker
        self.assertFalse(pause_marker(self.learning).exists())
        self.assertEqual(self.model.learning_state(), "on")

    def test_forget_and_promote(self):
        self.learn()
        self.assertEqual(self.model.forget("かた", "肩").message, "忘れました: かた → 肩")
        self.assertFalse(self.model.forget("かた", "肩").ok)
        self.assertEqual([p.surface for p in self.model.learned()], ["ヌエ"])
        self.assertFalse(self.model.promote("ぬえ", "鵺").ok)  # never learned
        self.assertFalse(self.model.promote("ぬえ", "ヌエ", "動詞").ok)
        self.assertTrue(self.model.promote("ぬえ", "ヌエ", "固有名詞").ok)
        self.assertEqual([(w.reading, w.surface, w.pos) for w in read_words(self.personal)],
                         [("ぬえ", "ヌエ", "固有名詞")])

    def test_no_learning_setting_is_reported(self):
        self.assertTrue(self.model.set_setting("no_learning", "オン").ok)
        self.assertEqual(self.model.learning_state(), "off")


class SettingsTest(Base):
    def rows(self):
        return {r.name: r for r in self.model.setting_rows()}

    def test_every_config_setting_has_japanese_text(self):
        # A new engine option must be given a name and a description here.
        self.assertEqual(set(SETTINGS), set(config.file_settings(argument_parser())))
        for name, text in SETTINGS.items():
            for shown in (text.label, text.help, *text.values.values()):
                self.assertFalse(shown.isascii(), (name, shown))

    def test_values_are_shown_in_japanese(self):
        rows = self.rows()
        self.assertEqual(list(rows), list(SETTINGS))
        self.assertEqual(rows["learning_file"].value, str(self.learning))  # stored in the file
        self.assertEqual(rows["lm_deadline_ms"].value, "400（既定）")
        self.assertEqual(rows["lm_prefetch"].value, "親指キーを押したとき（既定）")
        self.assertEqual(rows["lm_prefetch"].choices,
                         ["親指キーを押したとき", "キーを打つたび", "先読みしない"])
        self.assertEqual(rows["log_text"].value, "オフ（既定）")
        # A default of None is what the engine acts as: shown, and offered for editing.
        for name, value, edit in (("learning", "学習する（既定）", "学習する"),
                                  ("lm_rescore", "使う（既定）", "使う"),
                                  ("lm_alpha", "1.0（既定）", "1.0"),
                                  ("lm_k", "8（既定）", "8"),
                                  ("lm_threads", "自動（既定）", ""),  # empty means automatic
                                  ("lm_model", "標準のモデル（既定）", "")):
            self.assertEqual((rows[name].value, rows[name].edit), (value, edit), name)
        self.assertEqual(rows["lm_k"].choices, [])  # free text
        self.assertIn(rows["learning"].edit, rows["learning"].choices)

    def test_edit_round_trips_through_the_engine_parser(self):
        for name, shown in (("lm_deadline_ms", "250"), ("lm_prefetch", "キーを打つたび"),
                            ("lm_rescore", "使わない"), ("lm_k", "4"), ("log_text", "オン")):
            outcome = self.model.set_setting(name, shown)
            self.assertTrue(outcome.ok, outcome.message)
            self.assertTrue(outcome.message.endswith(admin_ui.RESTART_NOTE))
        args = config.parse_args(argument_parser(), ["--runtime-dir", str(self.dir)])
        self.assertEqual((args.lm_deadline_ms, args.lm_prefetch, args.lm_rescore, args.lm_k,
                          args.log_text), (250.0, "typing", False, 4, True))
        self.assertEqual(args.learning_file, self.learning)  # other keys kept
        rows = self.rows()
        self.assertEqual((rows["lm_prefetch"].value, rows["lm_prefetch"].edit),
                         ("キーを打つたび", "キーを打つたび"))
        self.assertEqual(rows["lm_deadline_ms"].value, "250")
        self.assertEqual(self.model.set_setting("lm_prefetch", "キーを打つたび").message,
                         "「言語モデルの先読み」を「キーを打つたび」にしました。"
                         + admin_ui.RESTART_NOTE)

    def test_invalid_values_are_refused_and_not_written(self):
        before = self.config.read_text(encoding="utf-8")
        for name, shown, message in (
                ("lm_deadline_ms", "soon", "「言語モデルを待つ上限（ミリ秒）」には数値を入力してください"),
                ("lm_prefetch", "always", "「言語モデルの先読み」には一覧から選んでください"),
                ("no_learning", "maybe", "「学習を無効にする（旧設定）」には一覧から選んでください"),
                ("lm_k", "2.5", "「言語モデルで比べる候補の数」には整数を入力してください")):
            outcome = self.model.set_setting(name, shown)
            self.assertEqual((outcome.ok, outcome.message), (False, message))
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)

    def test_reset_and_empty_text_return_to_default(self):
        self.model.set_setting("lm_k", "4")
        self.assertTrue(self.model.set_setting("lm_k", "  ").ok)
        self.assertEqual(self.rows()["lm_k"].value, "8（既定）")
        self.assertEqual(self.model.reset_setting("lm_k").message,
                         "「言語モデルで比べる候補の数」は既定値のままです")

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
        with redirect_stderr(io.StringIO()):  # the engine's "unknown setting" note
            self.model.set_setting("lm_k", "4")
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["future_option"], 1)

    def test_absent_config_file_is_created_on_first_edit(self):
        self.config.unlink()
        model = AdminModel(runtime_dir=self.dir)
        self.assertTrue(model.set_setting("lm_k", "4").ok)
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8")), {"lm_k": 4})

    def test_broken_config_file_is_reported_not_overwritten(self):
        self.config.write_text("[1, 2]", encoding="utf-8")
        with redirect_stderr(io.StringIO()):  # the engine's "not an object" note
            model = AdminModel(runtime_dir=self.dir)
            for outcome in (model.set_setting("lm_k", "4"), model.reset_setting("lm_k")):
                self.assertEqual((outcome.ok, outcome.message),
                                 (False, f"設定ファイル {self.config} が壊れているため保存できません"))
        self.assertEqual(self.config.read_text(encoding="utf-8"), "[1, 2]")


class StatusTest(Base):
    def test_status_in_japanese(self):
        self.learn()
        self.model.add_word("おやゆび", "親指")
        self.model.toggle_pause()
        self.broker["status"] = {"state": "ready", "warnings": ["lm"]}
        rows = dict(self.model.status_rows())
        self.assertEqual(rows["変換エンジン"], "動作中")
        self.assertEqual(rows["注意"], "言語モデルを使えません（辞書だけで変換しています）")
        self.assertEqual(rows["最後に止まった理由"], "なし")
        self.assertEqual(rows["学習"], "一時停止中")
        self.assertEqual(rows["学習した変換"], "2 件")
        self.assertEqual(rows["　うち単語として覚えたもの"], "1 件")
        self.assertEqual(rows["学習履歴の行数"], "2 行")
        self.assertEqual(rows["学習履歴の読めなかった行"], "0 行")
        self.assertEqual(rows["登録した単語"], "1 件")
        self.assertEqual(rows["個人辞書の読めなかった行"], "0 行")
        self.assertEqual(rows["設定ファイル"], str(self.config))
        self.assertIn(rows["辞書"], ("正常", "照合の記録（マニフェスト）がありません"))

    def test_no_broker_and_a_last_stop(self):
        logs = self.dir / "logs"
        logs.mkdir()
        (logs / "notices.log").write_text(
            json.dumps({"time": "2026-11-14T16:00:00", "stopped": "hang"}) + "\n",
            encoding="utf-8")
        rows = dict(self.model.status_rows())
        self.assertEqual(rows["変換エンジン"], "ブローカーが動いていません")
        self.assertEqual(rows["注意"], "なし")
        self.assertEqual(rows["最後に止まった理由"],
                         "変換エンジンが反応しなくなった（2026-11-14T16:00:00）")
        self.assertEqual(rows["学習した変換"], "0 件（学習履歴はまだありません）")

    def test_dictionary_problems_in_japanese(self):
        self.assertEqual(admin_ui._dictionary_problem("missing dictionary03.txt"),
                         "dictionary03.txt がありません")
        self.assertEqual(admin_ui._dictionary_problem("changed suffix.txt"),
                         "suffix.txt が変わっています")
        self.assertEqual(admin_ui._dictionary_problem(
            "unrecorded dictionary10.txt (the loader would read it)"),
            "dictionary10.txt は記録にないファイルです")


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
        modules = {"tkinter": tk, "tkinter.ttk": tk.ttk, "tkinter.messagebox": tk.messagebox,
                   "tkinter.font": tk.font}
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        tk.messagebox.askyesno.return_value = True
        tk.font.nametofont.return_value.metrics.return_value = 16
        tk.ttk.Treeview.side_effect = lambda *a, **k: mock.MagicMock()  # one per table
        root = mock.MagicMock()
        root.winfo_fpixels.return_value = 144.0  # a 150 % monitor
        w = admin_ui.AdminWindow(root, self.model)
        self.assertEqual(w.px(100), 150)
        return w

    def test_window_icon_is_the_product_icon(self):
        self.assertTrue(admin_ui.ICON.is_file())
        with mock.patch.object(admin_ui.sys, "platform", "win32"):
            w = self.window()
        w.root.iconbitmap.assert_called_once_with(default=str(admin_ui.ICON))

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
        self.select(w, w.learning_tree, ("ぬえ", "ヌエ", "1", "", "○"))
        w._promote()
        self.assertEqual([x.surface for x in read_words(self.personal)], ["ヌエ"])
        w._forget()
        self.assertEqual([p.surface for p in self.model.learned()], ["肩"])
        w.settings_tree.selection.return_value = (str(list(SETTINGS).index("lm_k")),)
        w._pick_setting()
        self.assertEqual((w.s_label.get(), w.s_value.get()), ("言語モデルで比べる候補の数", "8"))
        self.assertEqual(w.s_help.get(), SETTINGS["lm_k"].help)  # whole, not cut by the column
        w.s_value.set("4")
        w._save_setting()
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["lm_k"], 4)
        self.assertIn("再起動", w.message.get())
        w._reset_setting()
        self.assertNotIn("lm_k", json.loads(self.config.read_text(encoding="utf-8")))
        w._tab_changed()
        self.assertEqual(w.message.get(), "")  # a message stays with its tab
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
