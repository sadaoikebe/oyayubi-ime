"""The conversion engine (REDESIGN §6): one JSON object per line on stdin/stdout.

Started only by the broker (src/broker), as ``python -m yuragi.engine``. It has
no other way to talk: no sockets, no files besides the user's data and its own
program files. The broker reads stderr into logs/engine.log.

Broker → engine:
  {"op":"open","sid":3}                    a TIP connection's session
  {"op":"close","sid":3}
  {"sid":3,"seq":17,"op":"down","vk":74,"t":123456,"mods":[]}
      op as for yuragi.ime.server: down/up/timeout/reset/set_direct/toggle_direct/context
Engine → broker:
  {"progress":"cache"}                     while the dictionary cache is rebuilt (every 5 s)
  {"ready":true,"proto":1,"warnings":[...]}
  {"sid":3,"seq":17, ...the reply of yuragi.ime.server...}   one per request, in order
      "error":true   the request raised; the session was started afresh (§6.4)
      "full":true    the input is at MAX_TOKENS; the key changed nothing (§6.5)
  {"event":"warning","warning":"lm"}       a problem found after ready (once each)

Warnings: config (config.json partly unreadable, defaults used), lm (the LM is not
used), learning-read (the history could not be read), learning-write (could not be
saved). open/close have no reply. The engine exits when stdin closes.

Startup failures end the process with a reason on stderr, ``fatal <code> <reason>``:
20 a program file cannot be read, 21 a dictionary file is missing or wrong,
22 the dictionary cache cannot be rebuilt (§6.6).
"""
from __future__ import annotations

import argparse
from datetime import datetime
import gc
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback

PROTO = 1
MAX_TOKENS = 200  # REDESIGN §11: the input length a conversion handles
PROGRESS_SECONDS = 5
EXIT_PROGRAM = 20
EXIT_DICTIONARY = 21
EXIT_CACHE = 22
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = Path.home() / ".yuragi-ime"
# Warm-up input (§6.3): かいはつしてきた on the NICOLA-A plain layer, then Space.
WARM_KEYS = (0x57, 0x4C, 0x48, 0x4F, 0x53, 0x44, 0x4B, 0x45)


def log(message: str) -> None:
    """One line on stderr (the broker keeps it in engine.log). Never input text."""
    stamp = datetime.now().isoformat(timespec="milliseconds")
    print(f"{stamp} engine: {message}", file=sys.stderr, flush=True)


def fatal(code: int, reason: str):
    log(f"stopping: {reason}")
    print(f"fatal {code} {reason}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def where(exc: BaseException) -> str:
    """The exception's type and its frames, innermost last; no message (it may hold text)."""
    frames = traceback.extract_tb(exc.__traceback__)
    path = " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(frames))
    return f"{type(exc).__name__} at {path}"


class Channel:
    """The engine's half of the pipe: the real stdout, reserved for messages.

    Python's sys.stdout (and file descriptor 1) point at stderr instead, so a
    library that prints cannot corrupt the protocol.
    """

    def __init__(self):
        self._out = os.fdopen(os.dup(1), "wb", buffering=0)
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        self._lock = threading.Lock()
        self._ready = False
        self._warnings: list[str] = []

    def send(self, message: dict) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            self._out.write(data)

    def warning(self, code: str) -> None:
        """A problem the user is told about once (§9.3); kept until ready."""
        with self._lock:
            if code in self._warnings:
                return
            self._warnings.append(code)
            if not self._ready:
                return
        log(f"warning {code}")
        self.send({"event": "warning", "warning": code})

    def ready(self) -> None:
        with self._lock:
            self._ready = True
            warnings = list(self._warnings)
        for code in warnings:
            log(f"warning {code}")
        self.send({"ready": True, "proto": PROTO, "warnings": warnings})


class Progress:
    """{"progress":"cache"} now and every PROGRESS_SECONDS until stopped."""

    def __init__(self, channel: Channel):
        self.channel = channel
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        log("rebuilding the dictionary cache")
        self._thread = threading.Thread(target=self._run, name="progress", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            self.channel.send({"progress": "cache"})
            if self._stop.wait(PROGRESS_SECONDS):
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m yuragi.engine",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="the user's data: config.json, learning.jsonl, "
                             "personal_lexicon.jsonl (default ~/.yuragi-ime)")
    parser.add_argument("--dict-dir", type=Path, default=None,
                        help="the Mozc dictionary files (default third_party/mozc_oss_dict)")
    parser.add_argument("--cache-dir", type=Path, default=None,
                        help="where the dictionary cache is kept (default data/cache)")
    parser.add_argument("--lm-model", default=None,
                        help="the installed LM model directory (engine.json); a lm_model "
                             "in config.json still wins")
    parser.add_argument("--test-hooks", action="store_true",
                        help="accept {\"op\":\"test_raise\"}, which raises inside a request")
    return parser


class Connection:
    """One TIP connection's session (and its LM pipeline)."""

    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.sess = engine.new_session()
        self.pipeline = engine.new_pipeline(self.sess)

    def handle(self, msg: dict) -> dict:
        from yuragi.ime.server import process_request, snapshot
        if msg.get("op") == "test_raise" and self.engine.test_hooks:
            raise RuntimeError("test hook")
        if self.pipeline is not None:
            reply = self.pipeline.handle(msg)
        else:
            reply = process_request(self.sess, msg, log_text=self.engine.log_text,
                                    lm_timing=self.engine.lm_timing)
        sess = self.sess
        if len(sess.tokens) > MAX_TOKENS and not sess.converted and sess._await is None:
            # §6.5: the key is taken, the input stays as it was.
            del sess.tokens[MAX_TOKENS:]
            sess._refresh()
            commit = reply["commit"]
            reply = snapshot(sess, msg.get("op"))
            reply.update(commit=commit, pending=False, full=True)
        return reply

    def recover(self) -> dict:
        """§6.4: drop the session that raised; continue from a clean one."""
        from yuragi.ime.server import snapshot
        direct = self.sess.direct
        self.close()
        self.sess = self.engine.new_session()
        self.sess.direct = direct
        self.pipeline = self.engine.new_pipeline(self.sess)
        reply = snapshot(self.sess, None)
        reply.update(commit="", pending=False)
        return reply

    def close(self) -> None:
        if self.pipeline is not None:
            self.pipeline.close()


class Engine:
    def __init__(self, channel: Channel, args: argparse.Namespace):
        self.channel = channel
        self.test_hooks = args.test_hooks
        self.connections: dict[object, Connection] = {}
        self.loaded = None
        self.worker = None
        self.settings = None
        self.log_text = self.lm_timing = False

    # -- startup -----------------------------------------------------------------
    def load(self, args: argparse.Namespace) -> None:
        try:
            from yuragi.lattice import mozc_dict
            if args.dict_dir is not None:
                mozc_dict.DICT_DIR = args.dict_dir
            if args.cache_dir is not None:
                mozc_dict.CACHE = args.cache_dir / mozc_dict.CACHE.name
            # Imported after the directories are set: some modules read them at import.
            from yuragi.ime import config, server
            from yuragi.ime.lm_async import LmWorker
            from yuragi.lattice import dict_manifest
        except (ImportError, SyntaxError, OSError, ValueError) as exc:
            log(where(exc))
            fatal(EXIT_PROGRAM, "program-files-unreadable")

        manifest = json.loads(mozc_dict.MANIFEST.read_text(encoding="utf-8"))
        problems = dict_manifest.verify(manifest)
        if problems:
            for problem in problems:
                log(f"dictionary: {problem}")
            fatal(EXIT_DICTIONARY, "dictionary-missing"
                  if any(p.startswith("missing") for p in problems) else "dictionary-changed")

        settings = self.read_settings(args, config, server)
        self.settings = settings
        self.log_text, self.lm_timing = settings.log_text, settings.lm_timing

        progress = Progress(self.channel)
        try:
            lex = mozc_dict.load_lex(on_rebuild=progress.start)
        except (FileNotFoundError, mozc_dict.DictionaryFormatError) as exc:
            log(where(exc))
            fatal(EXIT_DICTIONARY, "dictionary-unreadable")
        except mozc_dict.CacheWriteError as exc:
            log(f"{where(exc)} (errno {exc.errno})")
            fatal(EXIT_CACHE, "cache-not-written")
        finally:
            progress.stop()

        loaded = server.load_session(settings, with_lm=False, lex=lex,
                                     create_learning_file=True,
                                     on_problem=self.channel.warning)
        store = loaded._learning
        if store is not None:
            log(f"learning {config.learning_mode(settings)}: {store.lines} lines, "
                f"{len(store.records())} live, {store.skipped} skipped")
            try:
                compacted = store.compact_if_needed()
            except OSError as exc:
                log(f"learning history not compacted: {where(exc)}")
            else:
                if compacted is not None:
                    log("learning history compacted: %d -> %d lines" % compacted)
        if loaded.personal is not None and loaded.personal.skipped:
            log(f"personal lexicon: {loaded.personal.skipped} line(s) skipped")
        self.loaded = loaded
        if settings.lm_rescore:
            self.worker = LmWorker(loader=lambda: self.load_lm(server, settings),
                                   lex=loaded._lex, on_disabled=self.lm_disabled)
        else:
            log("LM rescoring off (setting)")

    def read_settings(self, args, config, server) -> argparse.Namespace:
        """config.json in the data directory over the built-in defaults (§6.2)."""
        parser = server.argument_parser()
        parser.set_defaults(learning_file=args.data_dir / "learning.jsonl",
                            personal_file=args.data_dir / "personal_lexicon.jsonl")
        if args.lm_model is not None:
            parser.set_defaults(lm_model=args.lm_model)
        path = args.data_dir / config.CONFIG_NAME
        problems: list[str] = []
        parser.set_defaults(**config.read_config(path, parser, problems))
        settings = parser.parse_args([])
        settings.config = path
        if settings.lm_rescore is None:
            settings.lm_rescore = True  # the product path: on unless config.json says otherwise
        if problems:
            self.channel.warning("config")
        return settings

    def load_lm(self, server, settings):
        """On the LM worker: the model is read after ready; conversions use the
        Viterbi 1-best until it is there."""
        start = time.perf_counter()
        rescorer = server.build_rescorer(settings)
        log(f"LM loaded in {time.perf_counter() - start:.1f} s")
        return rescorer

    def lm_disabled(self, reason: str) -> None:
        log(f"LM rescoring not used ({reason})")
        self.channel.warning("lm")

    def warm_up(self) -> None:
        """§6.3: one conversion before ready, so the user's first key is not the slow one."""
        from yuragi.ime.server import process_request
        start = time.perf_counter()
        sess = self.new_session()
        t = 0
        for vk in WARM_KEYS:
            process_request(sess, {"op": "down", "vk": vk, "t": t})
            process_request(sess, {"op": "up", "vk": vk, "t": t + 20})
            t += 300
        process_request(sess, {"op": "down", "vk": 0x20, "t": t})
        reply = process_request(sess, {"op": "up", "vk": 0x20, "t": t + 20})
        process_request(sess, {"op": "reset"})
        log(f"warm-up conversion {1000 * (time.perf_counter() - start):.0f} ms "
            f"(converted: {reply['converted']})")

    # -- sessions ----------------------------------------------------------------
    def new_session(self):
        from yuragi.ime.session import ImeSession
        loaded = self.loaded
        return ImeSession(
            _table=loaded._table, _lex=loaded._lex, learning_path=loaded.learning_path,
            _learning=loaded._learning, personal=loaded.personal,
            prediction=loaded.prediction, predict_index=loaded.predict_index)

    def new_pipeline(self, sess):
        if self.worker is None:
            return None
        from yuragi.ime.lm_async import ConnectionPipeline
        s = self.settings
        return ConnectionPipeline(sess, self.worker, log_text=s.log_text,
                                  lm_timing=s.lm_timing, grace_ms=s.lm_grace_ms,
                                  deadline_ms=s.lm_deadline_ms, prefetch=s.lm_prefetch,
                                  prefetch_delay_ms=s.lm_prefetch_delay_ms)

    # -- serving -----------------------------------------------------------------
    def serve(self, stream) -> None:
        for raw in stream:
            if not raw.strip():
                continue
            try:
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    raise ValueError("not an object")
            except ValueError as exc:  # UnicodeDecodeError is a ValueError
                log(f"unreadable message from the broker ({type(exc).__name__}); ignored")
                continue
            op = msg.get("op")
            if op == "open":
                self.connections[msg.get("sid")] = Connection(self)
            elif op == "close":
                connection = self.connections.pop(msg.get("sid"), None)
                if connection is not None:
                    connection.close()
            else:
                self.channel.send(self.request(msg))

    def request(self, msg: dict) -> dict:
        """One request; whatever goes wrong inside stays inside this one (§6.4)."""
        sid, seq = msg.get("sid"), msg.get("seq")
        connection = self.connections.get(sid)
        if connection is None:
            log(f"request for a session that is not open (op {msg.get('op')!r})")
            reply = {"error": True}
        else:
            try:
                reply = connection.handle(msg)
            except Exception as exc:
                log(f"request failed: {where(exc)}; session started afresh")
                reply = connection.recover()
                reply["error"] = True
        reply["sid"] = sid
        reply["seq"] = seq
        return reply


def main(argv=None) -> int:
    channel = Channel()
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
    started = time.perf_counter()
    args = argument_parser().parse_args(argv)
    log(f"start pid {os.getpid()} python {sys.version.split()[0]} proto {PROTO}")
    engine = Engine(channel, args)
    engine.load(args)
    engine.warm_up()
    gc.freeze()  # the dictionary must not be traversed by full collections while typing
    channel.ready()
    log(f"ready in {time.perf_counter() - started:.2f} s")
    engine.serve(sys.stdin.buffer)
    log("stdin closed; exiting")
    if engine.worker is not None:
        engine.worker.stop()
        engine.worker.thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
