"""The management window: word registration, learning, settings, status (S4).

  python -m oyayubi.ime.admin_ui [--runtime-dir DIR] [--config FILE]

A thin shell over the same code as the manage CLI (personal, learning_admin,
config), on the same files the engine uses. AdminModel holds everything the
window does and imports no tkinter, so it is tested without a display;
AdminWindow only lays out widgets and forwards to it.

Everything the window shows is Japanese: setting names, values, descriptions,
status and messages come from the tables here, never from the option names or
the CLI's English.

Word and learning changes reach a running engine at its next conversion or
keystroke; settings are read when the engine starts (状態タブ: 変換エンジンを再起動).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import sys

from . import broker_client, config, learning_admin, manage, personal
from .learning import LearningStore

POS_NAMES = list(personal.POS_IDS)
RESTART_NOTE = "変換エンジンを再起動すると反映されます（「状態」タブ）。"


@dataclass(frozen=True)
class Outcome:
    """What an action did, for the status line; ok=False is shown as an error."""
    ok: bool
    message: str


@dataclass(frozen=True)
class SettingText:
    label: str
    help: str
    values: dict[str, str]  # stored text -> shown; {} for free text
    unset: str = ""  # shown when the built-in default is "decided by the engine"


ON_OFF = {"true": "オン", "false": "オフ"}

# Every setting config.json can hold, in the order the window lists them.
SETTINGS: dict[str, SettingText] = {
    "learning": SettingText(
        "学習", "確定した変換を覚えて、次から上位に出します。一時停止中は、覚えた内容は"
        "使いますが、新しくは覚えません。",
        {"on": "学習する", "paused": "一時停止", "off": "使わない"}, "学習する"),
    "prediction": SettingText(
        "入力の予測", "変換する前の入力中に Tab キーを押すと、学習した内容と辞書から"
        "続きの候補を出します。",
        {"tab": "Tab キーで出す", "off": "使わない"}),
    "lm_rescore": SettingText(
        "言語モデル", "変換するとき、言語モデルで候補の順位を見直します。使わないときは"
        "辞書だけで変換します。",
        {"true": "使う", "false": "使わない"}, "使う"),
    "lm_prefetch": SettingText(
        "言語モデルの先読み", "変換を速くするため、変換キーを押す前から言語モデルの"
        "計算を始めておきます。",
        {"thumb": "親指キーを押したとき", "typing": "キーを打つたび", "off": "先読みしない"}),
    "lm_deadline_ms": SettingText(
        "言語モデルを待つ上限（ミリ秒）", "変換キーを押してからこの時間を過ぎても言語モデルの"
        "結果が出ないときは、辞書だけの結果を出します。", {}),
    "lm_grace_ms": SettingText(
        "変換キーでその場で待つ時間（ミリ秒）", "変換キーを押したあと、表示を変える前に"
        "言語モデルの結果を待つ時間です。間に合わないときは表示をそのままにして待ち続け、"
        "その間に打ったキーは結果が出てから順に処理します。", {}),
    "lm_prefetch_delay_ms": SettingText(
        "先読みを始めるまで（ミリ秒）", "「親指キーを押したとき」の先読みで、親指キーを"
        "押してから計算を始めるまでの時間です。", {}),
    "lm_model": SettingText(
        "言語モデルの種類", "使う言語モデル（Hugging Face のモデル名か、モデルのフォルダ）。",
        {}, "標準のモデル"),
    "lm_alpha": SettingText(
        "言語モデルの重み", "大きいほど、辞書より言語モデルの判断を重く見ます。", {}, "1.0"),
    "lm_k": SettingText(
        "言語モデルで比べる候補の数", "辞書の候補のうち、上位いくつを言語モデルで比べるか。",
        {}, "8"),
    "lm_threads": SettingText(
        "言語モデルの CPU スレッド数", "言語モデルの計算に使う CPU のスレッド数。", {}, "自動"),
    "personal_file": SettingText(
        "個人辞書のファイル", "「単語登録」で登録した単語を保存するファイル。", {}),
    "learning_file": SettingText(
        "学習履歴のファイル", "学習した内容を保存するファイル。", {}),
    "no_learning": SettingText(
        "学習を無効にする（旧設定）", "古い版の設定です。オンは「学習」を「使わない」に"
        "するのと同じです。", ON_OFF),
    "log_text": SettingText(
        "入力した文字をログに残す（開発用）", "入力中と確定した文字を変換エンジンのログに"
        "書きます。入力内容がファイルに残るので、普段はオフにします。", ON_OFF),
    "lm_timing": SettingText(
        "言語モデルの処理時間をログに残す（開発用）", "変換ごとの言語モデルの処理時間を"
        "変換エンジンのログに書きます（入力内容は書きません）。", ON_OFF),
}

LEARNING_STATES = {"on": "学習中", "paused": "一時停止中", "off": "使わない設定"}
ENGINE_STATES = {"ready": "動作中", "starting": "起動中", "down": "停止中",
                 "not-installed": "インストールされていません"}
WARNINGS = {
    "lm": "言語モデルを使えません（辞書だけで変換しています）",
    "config": "設定ファイルの一部を読めませんでした（その項目は既定値で動いています）",
    "learning-read": "学習履歴を読めませんでした（学習なしで動いています）",
    "learning-write": "学習を保存できませんでした（覚えた内容はこの間だけ有効です）",
}
STOP_REASONS = {
    "program-files": "必要なプログラムのファイルが見つからないか、壊れていた",
    "dictionary": "辞書のファイルが見つからないか、壊れていた",
    "cache": "辞書の準備ができなかった（ディスクの空きが足りない可能性）",
    "repeated": "変換エンジンが繰り返し止まった",
    "hang": "変換エンジンが反応しなくなった",
}


@dataclass(frozen=True)
class SettingRow:
    name: str  # the key in config.json
    label: str
    value: str  # as shown, with （既定） when nothing is stored
    edit: str  # what the edit box starts with
    choices: list[str]  # shown values to offer; [] means free text
    help: str


def _stored_text(value) -> str:
    """A value as config.json would hold it, as text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _dictionary_problem(problem: str) -> str:
    kind, _, name = problem.partition(" ")
    if kind == "missing":
        return f"{name} がありません"
    if kind == "changed":
        return f"{name} が変わっています"
    if kind == "unrecorded":
        return f"{name.split(' ')[0]} は記録にないファイルです"
    return problem


class AdminModel:
    """The window's behaviour, on the engine's files; every change re-reads them."""

    def __init__(self, runtime_dir: Path | None = None, config_path: Path | None = None):
        self.runtime_dir = runtime_dir
        self.config_path = config_path
        self.reload()

    def reload(self) -> None:
        """Re-read config.json (it decides where the word and learning files are)."""
        from .server import argument_parser
        self.parser = argument_parser()
        self.settings = manage.server_settings(self.runtime_dir, self.config_path)

    # -- word registration (personal lexicon) --------------------------------

    @property
    def personal_file(self) -> Path:
        return self.settings.personal_file

    def words(self) -> list[personal.Word]:
        return personal.read_words(self.personal_file)

    def add_word(self, reading: str, surface: str, pos: str = "名詞",
                 cost: str = "") -> Outcome:
        reading, surface, cost = reading.strip(), surface.strip(), cost.strip()
        try:
            cost_value = int(cost) if cost else personal.DEFAULT_COST
        except ValueError:
            return Outcome(False, f"コストは整数で入力してください（「{cost}」は使えません）")
        try:
            word = personal.add_word(self.personal_file, reading, surface, pos, cost_value)
        except ValueError as exc:
            return Outcome(False, str(exc))
        return Outcome(True, f"登録しました: {word.reading} → {word.surface}"
                             f"（{word.pos}、コスト {word.cost}）")

    def remove_word(self, reading: str, surface: str) -> Outcome:
        if not personal.remove_word(self.personal_file, reading, surface):
            return Outcome(False, f"登録されていません: {reading} → {surface}")
        return Outcome(True, f"削除しました: {reading} → {surface}")

    # -- learning --------------------------------------------------------------

    @property
    def learning_file(self) -> Path:
        return self.settings.learning_file

    def learning_state(self) -> str:
        return learning_admin.state(self.settings)

    def learned(self, query: str = "") -> list[learning_admin.LearnedPair]:
        if not self.learning_file.exists():
            return []
        query = query.strip()
        return (learning_admin.search(self.learning_file, query) if query
                else learning_admin.pairs(self.learning_file))

    @staticmethod
    def learned_row(pair: learning_admin.LearnedPair) -> tuple[str, str, int, str, str]:
        """reading, surface, contexts, last confirmed, ○ if learned from kana as a word."""
        when = datetime.fromtimestamp(pair.last).strftime("%Y-%m-%d %H:%M")
        return (pair.reading, pair.surface, pair.contexts, when, "○" if pair.user_word else "")

    def learning_counts(self) -> str:
        pairs = self.learned()
        words = sum(p.user_word for p in pairs)
        return (f"学習: {LEARNING_STATES[self.learning_state()]} ・ 学習した変換 {len(pairs)} 件"
                f"（うち単語として覚えたもの {words} 件） ・ 登録した単語 {len(self.words())} 件")

    def can_toggle_pause(self) -> bool:
        return self.learning_state() != "off"

    def pause_label(self) -> str:
        return "学習を再開" if self.learning_state() == "paused" else "学習を一時停止"

    def toggle_pause(self) -> Outcome:
        """Pause or resume (the learning setting); off stays off."""
        state = self.learning_state()
        if state == "off":
            return Outcome(False, "「学習」の設定が「使わない」なので、一時停止はできません")
        learning_admin.set_mode(self.settings, "on" if state == "paused" else "paused")
        self.reload()
        done = "再開しました" if self.learning_state() == "on" else "一時停止しました"
        return Outcome(True, f"学習を{done}。{RESTART_NOTE}")

    def forget(self, reading: str, surface: str) -> Outcome:
        if not self.learning_file.exists():
            return Outcome(False, "学習履歴がまだありません")
        if not learning_admin.forget(self.learning_file, reading, surface):
            return Outcome(False, f"学習していません: {reading} → {surface}")
        return Outcome(True, f"忘れました: {reading} → {surface}")

    def promote(self, reading: str, surface: str, pos: str = "名詞") -> Outcome:
        if not self.learning_file.exists():
            return Outcome(False, "学習履歴がまだありません")
        try:
            word = learning_admin.promote(self.learning_file, self.personal_file,
                                          reading, surface, pos)
        except ValueError as exc:
            return Outcome(False, str(exc))
        if word is None:
            return Outcome(False, f"学習していません: {reading} → {surface}")
        return Outcome(True, f"単語登録しました: {word.reading} → {word.surface}（{word.pos}）。"
                             "学習履歴を消しても残ります")

    # -- settings --------------------------------------------------------------

    @property
    def config_file(self) -> Path:
        return self.settings.config

    def setting_rows(self) -> list[SettingRow]:
        stored = config.read_config(self.config_file, self.parser)
        actions = config.file_settings(self.parser)
        rows = []
        for name, text in SETTINGS.items():
            action = actions[name]
            value = stored[name] if name in stored else action.default
            raw = _stored_text(value)
            shown = text.values.get(raw, raw) if raw else text.unset
            edit = shown if raw else ""
            if name not in stored:
                shown += "（既定）"
            rows.append(SettingRow(name, text.label, shown, edit, list(text.values.values()),
                                   text.help))
        return rows

    def set_setting(self, name: str, shown: str) -> Outcome:
        """Write a value to config.json; empty text goes back to the default."""
        text = SETTINGS[name]
        if not shown.strip():
            return self.reset_setting(name)
        stored = {v: k for k, v in text.values.items()}.get(shown.strip(), shown)
        try:
            value = config.parse_setting(self.parser, name, stored)
        except ValueError:
            return Outcome(False, f"「{text.label}」には{self._expected(name)}")
        try:
            config.set_setting(self.config_file, self.parser, name, stored)
        except ValueError:
            return Outcome(False, f"設定ファイル {self.config_file} が壊れているため保存できません")
        self.reload()
        raw = _stored_text(value)
        return Outcome(True, f"「{text.label}」を「{text.values.get(raw, raw)}」にしました。"
                             f"{RESTART_NOTE}")

    def _expected(self, name: str) -> str:
        action = config.file_settings(self.parser)[name]
        if SETTINGS[name].values:
            return "一覧から選んでください"
        if action.type is int:
            return "整数を入力してください"
        if action.type is float:
            return "数値を入力してください"
        return "正しい値を入力してください"

    def reset_setting(self, name: str) -> Outcome:
        label = SETTINGS[name].label
        try:
            removed = config.unset_setting(self.config_file, name)
        except ValueError:
            return Outcome(False, f"設定ファイル {self.config_file} が壊れているため保存できません")
        if not removed:
            return Outcome(True, f"「{label}」は既定値のままです")
        self.reload()
        return Outcome(True, f"「{label}」を既定値に戻しました。{RESTART_NOTE}")

    # -- status ----------------------------------------------------------------

    def status_rows(self) -> list[tuple[str, str]]:
        """What the 状態 tab shows (REDESIGN §9.4), in Japanese."""
        from oyayubi.lattice import dict_manifest
        rows = []
        broker = broker_client.status()
        if broker is None:
            rows.append(("変換エンジン", "ブローカーが動いていません"))
        else:
            rows.append(("変換エンジン", ENGINE_STATES.get(broker["state"], broker["state"])))
        warnings = [WARNINGS.get(w, w) for w in (broker or {}).get("warnings", [])]
        rows.append(("注意", "、".join(warnings) if warnings else "なし"))
        runtime_dir = self.settings.runtime_dir or config.DEFAULT_RUNTIME_DIR
        stop = broker_client.last_stop(runtime_dir / "logs")
        rows.append(("最後に止まった理由",
                     f"{STOP_REASONS.get(stop['stopped'], stop['stopped'])}（{stop['time']}）"
                     if stop else "なし"))

        manifest = dict_manifest.MANIFEST
        if manifest.exists():
            problems = dict_manifest.verify(json.loads(manifest.read_text(encoding="utf-8")))
            rows.append(("辞書", "正常" if not problems else
                         "記録と違います: " + "、".join(map(_dictionary_problem, problems))))
        else:
            rows.append(("辞書", "照合の記録（マニフェスト）がありません"))

        rows.append(("学習", LEARNING_STATES[self.learning_state()]))
        if self.learning_file.exists():
            store = LearningStore(self.learning_file)
            pairs = learning_admin.pairs(self.learning_file)
            rows += [("学習した変換", f"{len(pairs)} 件"),
                     ("　うち単語として覚えたもの", f"{sum(p.user_word for p in pairs)} 件"),
                     ("学習履歴の行数", f"{store.lines} 行"),
                     ("学習履歴の読めなかった行", f"{store.skipped} 行")]
        else:
            rows.append(("学習した変換", "0 件（学習履歴はまだありません）"))
        words, skipped = personal.scan_words(self.personal_file)
        rows += [("登録した単語", f"{len(words)} 件"),
                 ("個人辞書の読めなかった行", f"{skipped} 行"),
                 ("設定ファイル", str(self.config_file)
                  + ("" if self.config_file.exists() else "（未作成。すべて既定値）")),
                 ("学習履歴のファイル", str(self.learning_file)),
                 ("個人辞書のファイル", str(self.personal_file))]
        return rows

    def restart_engine(self) -> Outcome:
        """REDESIGN §9.4: the broker's "retry" starts the engine again (new settings)."""
        if broker_client.retry():
            return Outcome(True, "変換エンジンを新しい設定で起動し直します")
        return Outcome(False, "ブローカーが動いていません")


def declare_dpi_aware() -> None:
    """Tell Windows this process draws for the monitor's scale itself.

    Without it the window is drawn at 96 dpi and stretched as a picture (blurred
    on a scaled 4K monitor). Tk then reads the real dpi and sizes its fonts.
    """
    if sys.platform == "win32":
        import ctypes
        ctypes.OleDLL("shcore").SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE


class AdminWindow:
    """Widgets only: every button calls AdminModel and redraws from it."""

    def __init__(self, root, model: AdminModel):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root, self.model = tk, ttk, root, model
        self._rows = {}  # tree -> {item id: row as given}
        self._setting_rows = {}  # settings tree item id -> SettingRow
        # Pixel sizes below are at 96 dpi; the window is dpi aware (declare_dpi_aware).
        self.scale = root.winfo_fpixels("1i") / 96
        root.title("oyayubi-ime 管理")
        root.geometry(f"{self.px(860)}x{self.px(540)}")
        from tkinter import font
        line = font.nametofont("TkDefaultFont").metrics("linespace")
        ttk.Style(root).configure("Treeview", rowheight=int(line * 1.4))
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=self.px(6), pady=self.px(6))
        self.message = tk.StringVar()
        self.message_label = ttk.Label(root, textvariable=self.message, anchor="w")
        self.message_label.pack(fill="x", padx=self.px(8), pady=(0, self.px(6)))
        for title, build in (("単語登録", self._words_tab), ("学習", self._learning_tab),
                             ("設定", self._settings_tab), ("状態", self._status_tab)):
            frame = ttk.Frame(notebook, padding=self.px(6))
            build(frame)
            notebook.add(frame, text=title)
        self.notebook, self.status_frame = notebook, frame
        notebook.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())
        self.refresh()

    def px(self, pixels: int) -> int:
        return round(pixels * self.scale)

    # helpers
    def _table(self, parent, columns, widths):
        ttk = self.ttk
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for col, width in zip(columns, widths):
            tree.heading(col, text=col)
            tree.column(col, width=self.px(width), stretch=col == columns[-1])
        bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        frame.pack(fill="both", expand=True)
        return tree

    def _fill(self, tree, rows):
        # The rows are kept here and read back by item id: Tk's own copy of the
        # values comes back converted (a surface "10" as the int 10).
        tree.delete(*tree.get_children())
        self._rows[tree] = {}
        for i, row in enumerate(rows):
            iid = str(i)
            self._rows[tree][iid] = tuple(row)
            tree.insert("", "end", iid=iid, values=row)

    def _selected(self, tree):
        sel = tree.selection()
        return self._rows.get(tree, {}).get(sel[0]) if sel else None

    def _show(self, outcome: Outcome):
        self.message.set(outcome.message)
        self.message_label.configure(foreground="" if outcome.ok else "red")
        self.refresh()

    def _tab_changed(self):
        # A message belongs to the tab it came from.
        self.message.set("")
        self.refresh()

    def _ask(self, question: str) -> bool:
        from tkinter import messagebox
        return messagebox.askyesno("oyayubi-ime", question, parent=self.root)

    # tabs
    def _words_tab(self, frame):
        tk, ttk = self.tk, self.ttk
        form = ttk.Frame(frame)
        form.pack(fill="x", pady=(0, self.px(6)))
        self.w_reading, self.w_surface = tk.StringVar(), tk.StringVar()
        self.w_pos, self.w_cost = tk.StringVar(value="名詞"), tk.StringVar()
        for label, var, width in (("読み", self.w_reading, 16), ("単語", self.w_surface, 16)):
            ttk.Label(form, text=label).pack(side="left")
            ttk.Entry(form, textvariable=var, width=width).pack(side="left", padx=(2, self.px(8)))
        ttk.Label(form, text="品詞").pack(side="left")
        ttk.Combobox(form, textvariable=self.w_pos, values=POS_NAMES, state="readonly",
                     width=9).pack(side="left", padx=(2, self.px(8)))
        ttk.Label(form, text="コスト").pack(side="left")
        ttk.Entry(form, textvariable=self.w_cost, width=6).pack(side="left", padx=(2, self.px(8)))
        ttk.Button(form, text="登録", command=self._add_word).pack(side="left")
        ttk.Button(form, text="選択を削除", command=self._remove_word).pack(side="right")
        ttk.Label(frame, text=f"コストは小さいほど上位に出ます（空欄なら {personal.DEFAULT_COST}）"
                  ).pack(fill="x", pady=(0, self.px(4)))
        self.words_tree = self._table(frame, ("読み", "単語", "品詞", "コスト"),
                                      (180, 220, 120, 80))

    def _add_word(self):
        outcome = self.model.add_word(self.w_reading.get(), self.w_surface.get(),
                                      self.w_pos.get(), self.w_cost.get())
        if outcome.ok:
            self.w_reading.set("")
            self.w_surface.set("")
        self._show(outcome)

    def _remove_word(self):
        row = self._selected(self.words_tree)
        if row and self._ask(f"{row[0]} → {row[1]} を削除しますか？"):
            self._show(self.model.remove_word(row[0], row[1]))

    def _learning_tab(self, frame):
        tk, ttk = self.tk, self.ttk
        top = ttk.Frame(frame)
        top.pack(fill="x", pady=(0, self.px(6)))
        self.l_query, self.l_pos = tk.StringVar(), tk.StringVar(value="名詞")
        ttk.Label(top, text="検索").pack(side="left")
        entry = ttk.Entry(top, textvariable=self.l_query, width=16)
        entry.pack(side="left", padx=(2, self.px(8)))
        entry.bind("<Return>", lambda e: self.refresh())
        ttk.Button(top, text="検索", command=self.refresh).pack(side="left")
        self.pause_button = ttk.Button(top, command=lambda: self._show(self.model.toggle_pause()))
        self.pause_button.pack(side="right")
        self.l_counts = tk.StringVar()
        ttk.Label(frame, textvariable=self.l_counts).pack(fill="x")
        actions = ttk.Frame(frame)
        actions.pack(side="bottom", fill="x", pady=(self.px(6), 0))
        ttk.Button(actions, text="選択を忘れる", command=self._forget).pack(side="left")
        ttk.Label(actions, text="品詞").pack(side="left", padx=(self.px(16), 2))
        ttk.Combobox(actions, textvariable=self.l_pos, values=POS_NAMES, state="readonly",
                     width=9).pack(side="left", padx=(0, self.px(4)))
        ttk.Button(actions, text="選択を単語登録", command=self._promote).pack(side="left")
        self.learning_tree = self._table(
            frame, ("読み", "変換", "文脈数", "最終確定", "単語として覚えた"),
            (160, 200, 70, 140, 110))

    def _forget(self):
        row = self._selected(self.learning_tree)
        if row and self._ask(f"{row[0]} → {row[1]} の学習を忘れますか？"):
            self._show(self.model.forget(row[0], row[1]))

    def _promote(self):
        row = self._selected(self.learning_tree)
        if row:
            self._show(self.model.promote(row[0], row[1], self.l_pos.get()))

    def _settings_tab(self, frame):
        tk, ttk = self.tk, self.ttk
        edit = ttk.Frame(frame)
        edit.pack(side="bottom", fill="x", pady=(self.px(6), 0))
        self.s_name, self.s_label, self.s_value = "", tk.StringVar(), tk.StringVar()
        ttk.Label(edit, textvariable=self.s_label).pack(side="left")
        self.s_box = ttk.Combobox(edit, textvariable=self.s_value, width=30)
        self.s_box.pack(side="left", padx=self.px(4))
        ttk.Button(edit, text="保存", command=self._save_setting).pack(side="left")
        ttk.Button(edit, text="既定に戻す", command=self._reset_setting
                   ).pack(side="left", padx=self.px(4))
        # The description column is cut at its width: the picked item's is shown here whole.
        self.s_help = tk.StringVar(value="項目を選ぶと、ここに説明が出て、下で変更できます。"
                                         "変更は、変換エンジンを再起動すると反映されます（「状態」タブ）。")
        ttk.Label(frame, textvariable=self.s_help, wraplength=self.px(820), justify="left"
                  ).pack(side="bottom", fill="x", pady=(self.px(6), 0))
        self.settings_tree = self._table(frame, ("項目", "値", "説明"), (260, 200, 400))
        self.settings_tree.bind("<<TreeviewSelect>>", self._pick_setting)

    def _pick_setting(self, _event=None):
        sel = self.settings_tree.selection()
        if not sel:
            return
        row = self._setting_rows[sel[0]]
        self.s_name = row.name
        self.s_label.set(row.label)
        self.s_help.set(row.help)
        self.s_value.set(row.edit)
        self.s_box.configure(values=row.choices, state="readonly" if row.choices else "normal")

    def _save_setting(self):
        if self.s_name:
            self._show(self.model.set_setting(self.s_name, self.s_value.get()))

    def _reset_setting(self):
        if self.s_name:
            self._show(self.model.reset_setting(self.s_name))

    def _status_tab(self, frame):
        ttk = self.ttk
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(0, self.px(6)))
        ttk.Button(buttons, text="表示を更新", command=self._refresh_status).pack(side="right")
        ttk.Button(buttons, text="変換エンジンを再起動",
                   command=lambda: self._show(self.model.restart_engine())
                   ).pack(side="right", padx=self.px(6))
        self.status_tree = self._table(frame, ("項目", "内容"), (240, 560))

    def refresh(self):
        m = self.model
        self._fill(self.words_tree, [(w.reading, w.surface, w.pos, w.cost) for w in m.words()])
        self._fill(self.learning_tree, [m.learned_row(p) for p in m.learned(self.l_query.get())])
        self.l_counts.set(m.learning_counts())
        self.pause_button.configure(text=m.pause_label(),
                                    state="normal" if m.can_toggle_pause() else "disabled")
        rows = m.setting_rows()
        self._fill(self.settings_tree, [(r.label, r.value, r.help) for r in rows])
        self._setting_rows = {str(i): r for i, r in enumerate(rows)}
        if self.notebook.select() == str(self.status_frame):
            self._refresh_status()

    def _refresh_status(self):
        # Hashes the dictionary files and asks the broker: only when looked at.
        self._fill(self.status_tree, self.model.status_rows())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime-dir", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        import tkinter as tk
    except ImportError:
        print("tkinter is not available in this Python; use python -m oyayubi.ime.manage",
              file=sys.stderr)
        return 1
    declare_dpi_aware()
    root = tk.Tk()
    AdminWindow(root, AdminModel(args.runtime_dir, args.config))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
