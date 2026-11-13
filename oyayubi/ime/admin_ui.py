"""The management window: word registration, learning, settings, status (S4).

  python -m oyayubi.ime.admin_ui [--runtime-dir DIR] [--config FILE]

A thin shell over the same code as the manage CLI (personal, learning_admin,
config, manage.status), on the same files the server uses. AdminModel holds
everything the window does and imports no tkinter, so it is tested without a
display; AdminWindow only lays out widgets and forwards to it.

As with the CLI, word and learning changes reach a running server at its next
conversion or keystroke; settings are read when the server starts.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import sys

from . import config, learning_admin, manage, personal

POS_NAMES = list(personal.POS_IDS)
RESTART_NOTE = "takes effect when the conversion server restarts"


@dataclass(frozen=True)
class Outcome:
    """What an action did, for the status line; ok=False is shown as an error."""
    ok: bool
    message: str


@dataclass(frozen=True)
class SettingRow:
    name: str
    value: str  # as shown and as typed back (paths and numbers as text)
    source: str  # "file" or "default"
    help: str
    switch: bool  # on/off: the value is true or false


def _shown(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def flatten(data, prefix: str = "") -> list[tuple[str, str]]:
    """manage.status()'s nested dict as (dotted key, text) rows, in its order."""
    if not isinstance(data, dict):
        return [(prefix, data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))]
    rows = []
    for key, value in data.items():
        rows += flatten(value, f"{prefix}.{key}" if prefix else str(key))
    return rows


class AdminModel:
    """The window's behaviour, on the server's files; every change re-reads them."""

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
            return Outcome(False, f"the cost must be a whole number, not {cost!r}")
        try:
            word = personal.add_word(self.personal_file, reading, surface, pos, cost_value)
        except ValueError as exc:
            return Outcome(False, str(exc))
        return Outcome(True, f"registered {word.reading} → {word.surface} "
                             f"({word.pos}, cost {word.cost})")

    def remove_word(self, reading: str, surface: str) -> Outcome:
        if not personal.remove_word(self.personal_file, reading, surface):
            return Outcome(False, f"not registered: {reading} → {surface}")
        return Outcome(True, f"removed {reading} → {surface}")

    # -- learning --------------------------------------------------------------

    @property
    def learning_file(self) -> Path:
        return self.settings.learning_file

    def learning_state(self) -> str:
        if self.settings.no_learning:
            return "off (no_learning setting)"
        return learning_admin.state(self.learning_file)

    def learned(self, query: str = "") -> list[learning_admin.LearnedPair]:
        if not self.learning_file.exists():
            return []
        query = query.strip()
        return (learning_admin.search(self.learning_file, query) if query
                else learning_admin.pairs(self.learning_file))

    @staticmethod
    def learned_row(pair: learning_admin.LearnedPair) -> tuple[str, str, int, str, str]:
        """reading, surface, contexts, last confirmed, "word" if learned from kana."""
        when = datetime.fromtimestamp(pair.last).strftime("%Y-%m-%d %H:%M")
        return (pair.reading, pair.surface, pair.contexts, when,
                "word" if pair.user_word else "")

    def learning_counts(self) -> str:
        pairs = self.learned()
        words = sum(p.user_word for p in pairs)
        return (f"learning {self.learning_state()} · {len(pairs)} learned pair(s), "
                f"{words} learned word(s) · {len(self.words())} registered word(s)")

    def can_toggle_pause(self) -> bool:
        return self.learning_file.exists()

    def pause_label(self) -> str:
        return "Resume learning" if learning_admin.state(self.learning_file) == "paused" \
            else "Pause learning"

    def toggle_pause(self) -> Outcome:
        if not self.learning_file.exists():
            return Outcome(False, f"learning is off (no {self.learning_file}); "
                                  "nothing to pause")
        paused = learning_admin.state(self.learning_file) == "paused"
        learning_admin.set_paused(self.learning_file, not paused)
        return Outcome(True, f"learning {learning_admin.state(self.learning_file)}")

    def forget(self, reading: str, surface: str) -> Outcome:
        if not self.learning_file.exists():
            return Outcome(False, f"learning is off (no {self.learning_file})")
        if not learning_admin.forget(self.learning_file, reading, surface):
            return Outcome(False, f"not learned: {reading} → {surface}")
        return Outcome(True, f"forgot {reading} → {surface}")

    def promote(self, reading: str, surface: str, pos: str = "名詞") -> Outcome:
        if not self.learning_file.exists():
            return Outcome(False, f"learning is off (no {self.learning_file})")
        try:
            word = learning_admin.promote(self.learning_file, self.personal_file,
                                          reading, surface, pos)
        except ValueError as exc:
            return Outcome(False, str(exc))
        if word is None:
            return Outcome(False, f"not learned: {reading} → {surface}")
        return Outcome(True, f"registered {word.reading} → {word.surface} ({word.pos}); "
                             "it now survives clearing the history")

    # -- settings --------------------------------------------------------------

    @property
    def config_file(self) -> Path:
        return self.settings.config

    def setting_rows(self) -> list[SettingRow]:
        actions = config.file_settings(self.parser)
        return [SettingRow(name, _shown(value), source, help_text,
                           config.is_switch(actions[name]))
                for name, value, source, help_text in config.effective(self.parser,
                                                                       self.config_file)]

    def setting_choices(self, name: str) -> list[str]:
        """Values to offer in a drop-down; [] means free text."""
        action = config.file_settings(self.parser)[name]
        if config.is_switch(action):
            return ["true", "false"]
        return [str(c) for c in action.choices] if action.choices else []

    @staticmethod
    def other_settings() -> list[tuple[str, str]]:
        return list(config.OTHER_SETTINGS.items())

    def set_setting(self, name: str, text: str) -> Outcome:
        """Write a value to config.json; empty text goes back to the default."""
        if not text.strip():
            return self.reset_setting(name)
        try:
            value = config.set_setting(self.config_file, self.parser, name, text)
        except ValueError as exc:
            return Outcome(False, str(exc))
        self.reload()
        return Outcome(True, f"{name} = {_shown(value)} saved in {self.config_file}; "
                             f"{RESTART_NOTE}")

    def reset_setting(self, name: str) -> Outcome:
        try:
            removed = config.unset_setting(self.config_file, name)
        except ValueError as exc:
            return Outcome(False, str(exc))
        if not removed:
            return Outcome(True, f"{name} already uses the built-in default")
        self.reload()
        return Outcome(True, f"{name} back to the built-in default; {RESTART_NOTE}")

    # -- status ----------------------------------------------------------------

    def status_rows(self) -> list[tuple[str, str]]:
        return flatten(manage.status(self.settings))


class AdminWindow:
    """Widgets only: every button calls AdminModel and redraws from it."""

    def __init__(self, root, model: AdminModel):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root, self.model = tk, ttk, root, model
        self._rows = {}  # tree -> {item id: row as given}
        root.title("oyayubi-ime 管理")
        root.geometry("820x520")
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=6, pady=6)
        self.message = tk.StringVar()
        self.message_label = ttk.Label(root, textvariable=self.message, anchor="w")
        self.message_label.pack(fill="x", padx=8, pady=(0, 6))
        for title, build in (("単語登録", self._words_tab), ("学習", self._learning_tab),
                             ("設定", self._settings_tab), ("状態", self._status_tab)):
            frame = ttk.Frame(notebook, padding=6)
            build(frame)
            notebook.add(frame, text=title)
        self.notebook, self.status_frame = notebook, frame
        notebook.bind("<<NotebookTabChanged>>", lambda e: self.refresh())
        self.refresh()

    # helpers
    def _table(self, parent, columns, widths):
        ttk = self.ttk
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for col, width in zip(columns, widths):
            tree.heading(col, text=col)
            tree.column(col, width=width, stretch=col == columns[-1])
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

    def _ask(self, question: str) -> bool:
        from tkinter import messagebox
        return messagebox.askyesno("oyayubi-ime", question, parent=self.root)

    # tabs
    def _words_tab(self, frame):
        tk, ttk = self.tk, self.ttk
        form = ttk.Frame(frame)
        form.pack(fill="x", pady=(0, 6))
        self.w_reading, self.w_surface = tk.StringVar(), tk.StringVar()
        self.w_pos, self.w_cost = tk.StringVar(value="名詞"), tk.StringVar()
        for label, var, width in (("読み", self.w_reading, 16), ("単語", self.w_surface, 16)):
            ttk.Label(form, text=label).pack(side="left")
            ttk.Entry(form, textvariable=var, width=width).pack(side="left", padx=(2, 8))
        ttk.Label(form, text="品詞").pack(side="left")
        ttk.Combobox(form, textvariable=self.w_pos, values=POS_NAMES, state="readonly",
                     width=9).pack(side="left", padx=(2, 8))
        ttk.Label(form, text="コスト").pack(side="left")
        ttk.Entry(form, textvariable=self.w_cost, width=6).pack(side="left", padx=(2, 8))
        ttk.Button(form, text="登録", command=self._add_word).pack(side="left")
        ttk.Button(form, text="選択を削除", command=self._remove_word).pack(side="right")
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
        top.pack(fill="x", pady=(0, 6))
        self.l_query, self.l_pos = tk.StringVar(), tk.StringVar(value="名詞")
        ttk.Label(top, text="検索").pack(side="left")
        entry = ttk.Entry(top, textvariable=self.l_query, width=16)
        entry.pack(side="left", padx=(2, 8))
        entry.bind("<Return>", lambda e: self.refresh())
        ttk.Button(top, text="検索", command=self.refresh).pack(side="left")
        self.pause_button = ttk.Button(top, command=lambda: self._show(self.model.toggle_pause()))
        self.pause_button.pack(side="right")
        self.l_counts = tk.StringVar()
        ttk.Label(frame, textvariable=self.l_counts).pack(fill="x")
        actions = ttk.Frame(frame)
        actions.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(actions, text="選択を忘れる", command=self._forget).pack(side="left")
        ttk.Label(actions, text="品詞").pack(side="left", padx=(16, 2))
        ttk.Combobox(actions, textvariable=self.l_pos, values=POS_NAMES, state="readonly",
                     width=9).pack(side="left", padx=(0, 4))
        ttk.Button(actions, text="選択を単語登録", command=self._promote).pack(side="left")
        self.learning_tree = self._table(
            frame, ("読み", "変換", "文脈数", "最終確定", "学習語"), (160, 200, 70, 140, 70))

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
        self.s_file = tk.StringVar()
        ttk.Label(frame, textvariable=self.s_file).pack(fill="x")
        edit = ttk.Frame(frame)
        edit.pack(side="bottom", fill="x", pady=(6, 0))
        self.s_name, self.s_value = tk.StringVar(), tk.StringVar()
        ttk.Label(edit, textvariable=self.s_name, width=22).pack(side="left")
        self.s_box = ttk.Combobox(edit, textvariable=self.s_value, width=40)
        self.s_box.pack(side="left", padx=4)
        ttk.Button(edit, text="保存", command=self._save_setting).pack(side="left")
        ttk.Button(edit, text="既定に戻す", command=self._reset_setting).pack(side="left", padx=4)
        ttk.Label(frame, text="設定は変換サーバの次回起動から有効").pack(side="bottom", fill="x")
        self.settings_tree = self._table(frame, ("項目", "値", "出所", "説明"),
                                         (170, 220, 60, 320))
        self.settings_tree.bind("<<TreeviewSelect>>", self._pick_setting)

    def _pick_setting(self, _event=None):
        row = self._selected(self.settings_tree)
        if not row or row[0] not in config.file_settings(self.model.parser):
            return
        self.s_name.set(row[0])
        self.s_value.set(row[1])
        self.s_box.configure(values=self.model.setting_choices(row[0]))

    def _save_setting(self):
        if self.s_name.get():
            self._show(self.model.set_setting(self.s_name.get(), self.s_value.get()))

    def _reset_setting(self):
        if self.s_name.get():
            self._show(self.model.reset_setting(self.s_name.get()))

    def _status_tab(self, frame):
        ttk = self.ttk
        ttk.Button(frame, text="更新", command=self._refresh_status).pack(anchor="e", pady=(0, 6))
        self.status_tree = self._table(frame, ("項目", "値"), (220, 520))

    def refresh(self):
        m = self.model
        self._fill(self.words_tree, [(w.reading, w.surface, w.pos, w.cost) for w in m.words()])
        self._fill(self.learning_tree, [m.learned_row(p) for p in m.learned(self.l_query.get())])
        self.l_counts.set(m.learning_counts())
        self.pause_button.configure(text=m.pause_label(),
                                    state="normal" if m.can_toggle_pause() else "disabled")
        self.s_file.set(f"{m.config_file}"
                        f"{'' if m.config_file.exists() else '（未作成：既定値で動作）'}")
        self._fill(self.settings_tree,
                   [(r.name, r.value, r.source, r.help) for r in m.setting_rows()]
                   + [(name, "", "他所", meaning) for name, meaning in m.other_settings()])
        if self.notebook.select() == str(self.status_frame):
            self._refresh_status()

    def _refresh_status(self):
        # Hashes the dictionary files and probes the server: only when looked at.
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
    root = tk.Tk()
    AdminWindow(root, AdminModel(args.runtime_dir, args.config))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
