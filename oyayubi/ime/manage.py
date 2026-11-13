"""Looking after the IME's own data from a terminal (D1, D2, D3, S1-S3).

  python -m oyayubi.ime.manage word add とうきょうとっきょ 東京特許 [--pos 固有名詞]
  python -m oyayubi.ime.manage word remove とうきょうとっきょ 東京特許
  python -m oyayubi.ime.manage word list
  python -m oyayubi.ime.manage learning list [QUERY]
  python -m oyayubi.ime.manage learning forget READING SURFACE
  python -m oyayubi.ime.manage learning promote READING SURFACE [--pos 名詞]
  python -m oyayubi.ime.manage learning compact
  python -m oyayubi.ime.manage learning clear [--with-words]
  python -m oyayubi.ime.manage learning pause | resume
  python -m oyayubi.ime.manage dict verify [--deep] | write --label LABEL
  python -m oyayubi.ime.manage config show | set KEY VALUE | unset KEY
  python -m oyayubi.ime.manage status

The files are the ones the server uses: its settings file (config.json in the
runtime directory) decides the paths, as for the server itself. A running
server picks up word changes at the next conversion and learning-file changes
at the next keystroke; no restart is needed.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from oyayubi.lattice import dict_manifest
from . import config, learning_admin, personal


def server_settings(runtime_dir: Path | None, config_path: Path | None) -> argparse.Namespace:
    from .server import argument_parser
    argv = []
    if runtime_dir is not None:
        argv += ["--runtime-dir", str(runtime_dir)]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    return config.parse_args(argument_parser(), argv)


learning_state = learning_admin.state


def _when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def cmd_word(args, settings) -> int:
    path = settings.personal_file
    if args.action == "add":
        word = personal.add_word(path, args.reading, args.surface, args.pos,
                                 personal.DEFAULT_COST if args.cost is None else args.cost)
        print(f"added {word.reading} → {word.surface} ({word.pos}, cost {word.cost}) to {path}")
    elif args.action == "remove":
        if not personal.remove_word(path, args.reading, args.surface):
            print(f"not registered: {args.reading} → {args.surface}")
            return 1
        print(f"removed {args.reading} → {args.surface}")
    else:
        for w in personal.read_words(path):
            print(f"{w.reading}\t{w.surface}\t{w.pos}\t{w.cost}")
    return 0


def cmd_learning(args, settings) -> int:
    path = settings.learning_file
    if args.action == "list":
        rows = (learning_admin.search(path, args.query) if args.query
                else learning_admin.pairs(path))
        for p in rows:
            mark = "\tword" if p.user_word else ""
            print(f"{p.reading}\t{p.surface}\t{p.contexts}\t{_when(p.last)}{mark}")
        return 0
    if args.action in ("pause", "resume"):
        if not path.exists():
            print(f"learning is off (no {path}); nothing to {args.action}")
            return 1
        learning_admin.set_paused(path, args.action == "pause")
        print(f"learning {learning_state(path)}")
        return 0
    if not path.exists():
        print(f"learning is off (no {path})")
        return 1
    if args.action == "forget":
        if not learning_admin.forget(path, args.reading, args.surface):
            print(f"not learned: {args.reading} → {args.surface}")
            return 1
        print(f"forgot {args.reading} → {args.surface}")
    elif args.action == "promote":
        word = learning_admin.promote(path, settings.personal_file, args.reading,
                                      args.surface, args.pos)
        if word is None:
            print(f"not learned: {args.reading} → {args.surface} (use: word add)")
            return 1
        print(f"registered {word.reading} → {word.surface} ({word.pos}) in "
              f"{settings.personal_file}; it now survives clearing the history")
    elif args.action == "compact":
        before, after = learning_admin.compact(path)
        print(f"compacted {path}: {before} → {after} lines")
    else:
        kept = learning_admin.clear_history(path, keep_user_words=not args.with_words)
        print(f"cleared {path}; kept {kept} learned word(s)")
    return 0


def cmd_dict(args, settings) -> int:
    argv = [args.action, "--manifest", str(args.manifest)]
    if args.action == "write":
        argv += ["--label", args.label, "--upstream-commit", args.upstream_commit]
    elif args.deep:
        argv.append("--deep")
    return dict_manifest.main(argv)


def cmd_config(args, settings) -> int:
    from .server import argument_parser
    path = settings.config
    if args.action == "set":
        value = config.set_setting(path, argument_parser(), args.key, args.value)
        shown = json.dumps(str(value) if isinstance(value, Path) else value, ensure_ascii=False)
        print(f"{args.key} = {shown} in {path}; takes effect when the server restarts")
        return 0
    if args.action == "unset":
        if not config.unset_setting(path, args.key):
            print(f"{args.key} is not set in {path}; it already uses the default")
            return 1
        print(f"{args.key} back to the default; takes effect when the server restarts")
        return 0
    print(f"# {path} ({'present' if path.exists() else 'absent: built-in defaults'})")
    for name, value, source, help_text in config.effective(argument_parser(), path):
        shown = json.dumps(str(value) if isinstance(value, Path) else value, ensure_ascii=False)
        print(f"{name} = {shown}\t[{source}]\t{help_text}")
    print("# kept elsewhere")
    for name, meaning in config.OTHER_SETTINGS.items():
        print(f"{name}\t{meaning}")
    return 0


def status(settings) -> dict:
    learning_path = settings.learning_file
    state = "off (--no-learning)" if settings.no_learning else learning_state(learning_path)
    pairs = learning_admin.pairs(learning_path) if learning_path.exists() else []
    manifest = dict_manifest.MANIFEST
    if manifest.exists():
        problems = dict_manifest.verify(json.loads(manifest.read_text(encoding="utf-8")))
        dictionary = "matches the manifest" if not problems else "; ".join(problems)
    else:
        dictionary = f"no manifest ({manifest.name}); write one with: dict write"
    from .client import connect
    runtime_dir = settings.runtime_dir or config.DEFAULT_RUNTIME_DIR
    try:
        with connect(runtime_dir) as client:
            resident = client.request({"op": "stats"}).get("stats")
    except (OSError, ValueError, KeyError, EOFError):
        resident = None
    return {
        "config": str(settings.config) + ("" if settings.config.exists() else " (absent)"),
        "learning": {"file": str(learning_path), "state": state, "pairs": len(pairs),
                     "learned_words": sum(p.user_word for p in pairs)},
        "personal_lexicon": {"file": str(settings.personal_file),
                             "words": len(personal.read_words(settings.personal_file))},
        "dictionary": dictionary,
        "resident_server": resident if resident is not None else "not running",
    }


def restart_server(settings) -> bool:
    """Ask the resident server to exit; True when it answered.

    Settings are read when the server starts, so this is how a settings
    change takes effect: the TIP respawns the server, with the new
    settings, on the next keystroke.
    """
    from .client import connect
    runtime_dir = settings.runtime_dir or config.DEFAULT_RUNTIME_DIR
    try:
        with connect(runtime_dir) as client:
            return bool(client.request({"op": "shutdown"}).get("ok"))
    except (OSError, ValueError, KeyError, EOFError):
        return False


def cmd_server(args, settings) -> int:
    if restart_server(settings):
        print("resident server stopped; it restarts with the new settings "
              "on the next keystroke")
    else:
        print("resident server is not running")
    return 0


def cmd_status(args, settings) -> int:
    print(json.dumps(status(settings), ensure_ascii=False, indent=2))
    return 0


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime-dir", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    word = sub.add_parser("word", help="the personal word list").add_subparsers(
        dest="action", required=True)
    add = word.add_parser("add")
    add.add_argument("reading")
    add.add_argument("surface")
    add.add_argument("--pos", default="名詞", choices=list(personal.POS_IDS))
    add.add_argument("--cost", type=int, default=None)
    remove = word.add_parser("remove")
    remove.add_argument("reading")
    remove.add_argument("surface")
    word.add_parser("list")

    learning = sub.add_parser("learning", help="the learning history").add_subparsers(
        dest="action", required=True)
    listing = learning.add_parser("list")
    listing.add_argument("query", nargs="?")
    for name in ("forget", "promote"):
        p = learning.add_parser(name)
        p.add_argument("reading")
        p.add_argument("surface")
        if name == "promote":
            p.add_argument("--pos", default="名詞", choices=list(personal.POS_IDS))
    learning.add_parser("compact")
    clear = learning.add_parser("clear")
    clear.add_argument("--with-words", action="store_true",
                       help="also erase the words learned from kana spellings")
    learning.add_parser("pause")
    learning.add_parser("resume")

    dictionary = sub.add_parser("dict", help="the dictionary manifest").add_subparsers(
        dest="action", required=True)
    verify = dictionary.add_parser("verify")
    verify.add_argument("--deep", action="store_true")
    write = dictionary.add_parser("write")
    write.add_argument("--label", required=True)
    write.add_argument("--upstream-commit", default="unknown")
    for p in (verify, write):
        p.add_argument("--manifest", type=Path, default=dict_manifest.MANIFEST)

    settings_cmd = sub.add_parser("config", help="settings").add_subparsers(
        dest="action", required=True)
    settings_cmd.add_parser("show")
    set_cmd = settings_cmd.add_parser("set", help="write one setting to config.json")
    set_cmd.add_argument("key")
    set_cmd.add_argument("value")
    settings_cmd.add_parser("unset", help="drop a setting (back to the default)"
                            ).add_argument("key")
    sub.add_parser("status", help="learning, words, dictionary, resident server")
    server_cmd = sub.add_parser("server", help="the resident server").add_subparsers(
        dest="action", required=True)
    server_cmd.add_parser("restart", help="stop it; it restarts with fresh settings")
    return parser


COMMANDS = {"word": cmd_word, "learning": cmd_learning, "dict": cmd_dict,
            "config": cmd_config, "status": cmd_status, "server": cmd_server}


def main(argv=None) -> int:
    args = argument_parser().parse_args(argv)
    settings = server_settings(args.runtime_dir, args.config)
    try:
        return COMMANDS[args.command](args, settings)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a code page
    sys.exit(main())
