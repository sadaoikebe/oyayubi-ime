"""One settings file for the conversion server (S1).

~/.oyayubi-ime/config.json holds any of the server's options under their
command-line names (lm_deadline_ms for --lm-deadline-ms). A value in the file
replaces the built-in default; an option given on the command line still wins.
No file means the built-in defaults, exactly as before.

The TIP keeps its own ~/.oyayubi-ime/tip.json (read by the C++ side); the
settings inventory below lists both so there is one place to look.

  python -m oyayubi.ime.manage config show   # effective values and their source
  python -m oyayubi.ime.admin_ui             # view and edit them in a window

The server reads the file when it starts: a changed setting takes effect at
the next server start.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

DEFAULT_RUNTIME_DIR = Path.home() / ".oyayubi-ime"  # the user's data: config.json, logs, ...
CONFIG_NAME = "config.json"

LEARNING_MODES = ("on", "paused", "off")  # config.json "learning" (REDESIGN §6.7)

# Options that only make sense per invocation, never from a file.
NOT_IN_FILE = {"runtime_dir", "config", "help"}

# Settings that live elsewhere, listed so the inventory is complete.
OTHER_SETTINGS = {
    "tip.json toggle_vk": "TIP: the direct-input toggle key (default VK_OEM_3, C++ TipConfig)",
    "tip.json toggle_mods": "TIP: its modifiers, e.g. [\"alt\"] (the default)",
    "tip.json log_text": "TIP: log composition text (default off)",
    "<learning file>.paused present": "before the learning setting: paused, when learning is unset",
    "ZERO_ROW_PENALTY (environment)": "evaluation knob of the dictionary loader, not a user setting",
}


def file_settings(parser: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    # The first action of a dest owns its default (--lm-rescore before --no-lm-rescore).
    actions: dict[str, argparse.Action] = {}
    for a in parser._actions:
        if a.dest not in NOT_IN_FILE:
            actions.setdefault(a.dest, a)
    return actions


def read_config(path: Path, parser: argparse.ArgumentParser, problems: list | None = None) -> dict:
    """The file's values, converted like the command line would; {} if absent.

    A file the user edited by hand never stops the IME (REDESIGN §6.2): broken
    JSON, a value of the wrong type, a value out of its choices or an unknown
    key each leave that setting at its default. What was wrong is printed to
    stderr and, when a list is given, appended to ``problems``.
    """
    def problem(text: str) -> None:
        print(f"{path}: {text}", file=sys.stderr)
        if problems is not None:
            problems.append(text)

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))  # a BOM is fine
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:  # UnicodeDecodeError is a ValueError
        problem(f"not readable as JSON ({type(exc).__name__}); built-in defaults used")
        return {}
    if not isinstance(raw, dict):
        problem("does not hold a JSON object; built-in defaults used")
        return {}
    known = file_settings(parser)
    values = {}
    for key, value in raw.items():
        action = known.get(key)
        if action is None:
            problem(f"unknown setting {key!r} ignored")
            continue
        if is_switch(action):
            if not isinstance(value, bool):
                problem(f"{key}={value!r} is not true or false; ignored")
                continue
        elif value is not None and action.type is not None:
            try:
                value = action.type(value)
            except (TypeError, ValueError):
                problem(f"{key}={value!r} is not a valid value; ignored")
                continue
        if action.choices is not None and value not in action.choices:
            problem(f"{key}={value!r} is not one of {list(action.choices)}; ignored")
            continue
        values[key] = value
    return values


def parse_args(parser: argparse.ArgumentParser, argv=None) -> argparse.Namespace:
    """Command line over config file over built-in defaults."""
    pre, _ = parser.parse_known_args(argv)
    path = pre.config if pre.config is not None else (
        (pre.runtime_dir or DEFAULT_RUNTIME_DIR) / CONFIG_NAME)
    parser.set_defaults(**read_config(path, parser))
    args = parser.parse_args(argv)
    args.config = path
    return args


def effective(parser: argparse.ArgumentParser, path: Path) -> list[tuple[str, object, str, str]]:
    """(name, value, source, help) for every server setting."""
    from_file = read_config(path, parser)
    rows = []
    for dest, action in file_settings(parser).items():
        if dest in from_file:
            value, source = from_file[dest], "file"
        else:
            value, source = action.default, "default"
        rows.append((dest, value, source, action.help or ""))
    return rows


def is_switch(action: argparse.Action) -> bool:
    """An on/off option (store_true): its file value is true or false."""
    return action.nargs == 0 and isinstance(action.const, bool)


def parse_setting(parser: argparse.ArgumentParser, key: str, text: str):
    """Text typed for a setting, converted and checked as read_config would."""
    action = file_settings(parser).get(key)
    if action is None:
        raise ValueError(f"unknown setting {key!r}")
    text = text.strip()
    if is_switch(action):
        lowered = text.lower()
        if lowered in ("true", "on", "yes", "1"):
            return True
        if lowered in ("false", "off", "no", "0"):
            return False
        raise ValueError(f"{key} is on or off (true/false), not {text!r}")
    try:
        value = action.type(text) if action.type is not None else text
    except (TypeError, ValueError):
        raise ValueError(f"{key}: {text!r} is not a valid value") from None
    if action.choices is not None and value not in action.choices:
        raise ValueError(f"{key} is one of {list(action.choices)}, not {text!r}")
    return value


def _read_raw(path: Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))  # a BOM is fine
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return raw


def _write_raw(path: Path, raw: dict) -> None:
    """Replace the file atomically; keys the server does not know stay as written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def set_setting(path: Path, parser: argparse.ArgumentParser, key: str, text: str):
    """Write one setting into the file; returns the value as stored."""
    value = parse_setting(parser, key, text)
    raw = _read_raw(path)
    raw[key] = str(value) if isinstance(value, Path) else value
    _write_raw(path, raw)
    return value


def unset_setting(path: Path, key: str) -> bool:
    """Drop a setting from the file (back to the built-in default)."""
    raw = _read_raw(path)
    if key not in raw:
        return False
    del raw[key]
    _write_raw(path, raw)
    return True


def learning_mode(settings: argparse.Namespace) -> str:
    """on, paused or off: the learning setting (REDESIGN §6.7).

    Unset, a pause marker left by an older version (<learning file>.paused)
    still means paused; no_learning (an older option) means off.
    """
    from .learning import pause_marker
    if settings.no_learning or settings.learning == "off":
        return "off"
    if settings.learning is None:
        return "paused" if pause_marker(settings.learning_file).exists() else "on"
    return settings.learning


def set_learning_mode(settings: argparse.Namespace, parser: argparse.ArgumentParser,
                      mode: str) -> None:
    """Store the learning setting; the old pause marker is then no longer needed."""
    from .learning import pause_marker
    set_setting(settings.config, parser, "learning", mode)
    pause_marker(settings.learning_file).unlink(missing_ok=True)
