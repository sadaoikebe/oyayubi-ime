"""One settings file for the conversion server (S1).

~/.oyayubi-ime/config.json holds any of the server's options under their
command-line names (lm_deadline_ms for --lm-deadline-ms). A value in the file
replaces the built-in default; an option given on the command line still wins.
No file means the built-in defaults, exactly as before.

The TIP keeps its own ~/.oyayubi-ime/tip.json (read by the C++ side); the
settings inventory below lists both so there is one place to look.

  python -m oyayubi.ime.manage config show   # effective values and their source
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

DEFAULT_RUNTIME_DIR = Path.home() / ".oyayubi-ime"  # endpoint, lock and config.json
CONFIG_NAME = "config.json"

# Options that only make sense per invocation, never from a file.
NOT_IN_FILE = {"resident", "runtime_dir", "config", "help"}

# Settings that live elsewhere, listed so the inventory is complete.
OTHER_SETTINGS = {
    "tip.json toggle_vk": "TIP: the direct-input toggle key (default VK_OEM_3, C++ TipConfig)",
    "tip.json toggle_mods": "TIP: its modifiers, e.g. [\"alt\"] (the default)",
    "tip.json log_text": "TIP: log composition text (default off)",
    "learning file present": "learning on/off: delete the file to turn learning off",
    "<learning file>.paused present": "learning paused: history kept and used, nothing recorded",
    "ZERO_ROW_PENALTY (environment)": "evaluation knob of the dictionary loader, not a user setting",
}


def file_settings(parser: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    # The first action of a dest owns its default (--lm-rescore before --no-lm-rescore).
    actions: dict[str, argparse.Action] = {}
    for a in parser._actions:
        if a.dest not in NOT_IN_FILE:
            actions.setdefault(a.dest, a)
    return actions


def read_config(path: Path, parser: argparse.ArgumentParser) -> dict:
    """The file's values, converted like the command line would; {} if absent.

    A key the server does not know is reported and skipped, so a typo in the
    file cannot keep the IME from starting.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    known = file_settings(parser)
    values = {}
    for key, value in raw.items():
        action = known.get(key)
        if action is None:
            print(f"{path}: unknown setting {key!r} ignored", file=sys.stderr)
            continue
        if value is not None and action.type is not None:
            value = action.type(value)
        if action.choices is not None and value not in action.choices:
            print(f"{path}: {key}={value!r} is not one of {list(action.choices)}; ignored",
                  file=sys.stderr)
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
