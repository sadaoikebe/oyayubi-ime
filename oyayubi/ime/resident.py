"""Authenticated loopback transport. Dictionaries live once, sessions per socket."""

from __future__ import annotations

import gc
import hmac
import json
import os
from pathlib import Path
import secrets
import socketserver
import threading
import time

from .lm_async import ConnectionPipeline, LmStats, LmWorker
from .runtime_platform import process_lock
from .server import build_rescorer, load_session, process_request
from .config import DEFAULT_RUNTIME_DIR
from .session import ImeSession

VERSION = 1
MAX_LINE = 1024 * 1024


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


class SessionFactory:
    def __init__(self, loaded: ImeSession):
        self.loaded = loaded

    def __call__(self) -> ImeSession:
        loaded = self.loaded
        return ImeSession(
            _table=loaded._table, _lex=loaded._lex, learning_path=loaded.learning_path,
            _learning=loaded._learning, personal=loaded.personal,
            prediction=loaded.prediction, predict_index=loaded.predict_index,
        )


def read_message(stream) -> dict:
    line = stream.readline(MAX_LINE + 1)
    if not line:
        raise EOFError
    if len(line) > MAX_LINE or not line.endswith(b"\n"):
        raise ValueError("invalid frame")
    msg = json.loads(line)
    if not isinstance(msg, dict):
        raise ValueError("expected object")
    return msg


def write_message(stream, msg: dict) -> None:
    stream.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    stream.flush()


class ConnectionHandler(socketserver.StreamRequestHandler):
    def handle(self):
        pipeline = None
        # Slow/unauthenticated peers must not allocate sessions or hold threads forever.
        self.request.settimeout(2.0)
        try:
            hello = read_message(self.rfile)
            token = hello.get("token")
            if (hello.get("op") != "hello" or hello.get("version") != VERSION
                    or not isinstance(token, str)
                    or not hmac.compare_digest(token.encode("utf-8"), self.server.token.encode("ascii"))):
                return
            self.request.settimeout(None)
            server = self.server
            with server.dispatch_lock:
                session = server.session_factory()
                if server.worker is not None:
                    pipeline = ConnectionPipeline(
                        session, server.worker, stats=server.stats, log_text=server.log_text,
                        lm_timing=server.lm_timing, **server.lm_options)
            write_message(self.wfile, {"ok": True, "msg": "ready", "version": VERSION})
            while True:
                msg = read_message(self.rfile)
                if msg.get("op") == "quit":
                    return
                if msg.get("op") == "stats":
                    write_message(self.wfile, {"ok": True, "stats": server.lm_stats()})
                    continue
                if msg.get("op") == "shutdown":
                    # A settings change needs a fresh server (settings are
                    # read at startup). The TIP respawns it on the next key.
                    write_message(self.wfile, {"ok": True, "msg": "bye"})
                    threading.Thread(target=server.shutdown, daemon=True).start()
                    return
                with server.dispatch_lock:
                    start = time.perf_counter()
                    if pipeline is not None:
                        reply = pipeline.handle(msg)
                    else:
                        reply = process_request(session, msg, log_text=server.log_text,
                                                lm_timing=server.lm_timing)
                    server.stats.observe("request_ms", (time.perf_counter() - start) * 1000)
                write_message(self.wfile, reply)
        except (EOFError, OSError, ValueError, TypeError, KeyError):
            # No request/error text in logs. Only this connection is discarded.
            return
        finally:
            if pipeline is not None:
                pipeline.close()


class ResidentServer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, session_factory, *, log_text=False, lm_timing=False, worker=None,
                 lm_options=None):
        """worker: an LmWorker; conversions then go through ConnectionPipeline.

        lm_options: ConnectionPipeline settings (grace_ms, deadline_ms, prefetch,
        prefetch_delay_ms). Without a worker, requests run synchronously.
        """
        self.session_factory = session_factory
        self.log_text = log_text
        self.lm_timing = lm_timing
        self.worker = worker
        self.lm_options = dict(lm_options or {})
        self.stats = worker.stats if worker is not None else LmStats()
        self.dispatch_lock = threading.Lock()
        self.token = secrets.token_hex(32)
        super().__init__(("127.0.0.1", 0), ConnectionHandler)

    def publish(self, directory: Path) -> Path:
        private_directory(directory)
        endpoint = directory / "endpoint.json"
        temporary = directory / ("endpoint-" + secrets.token_hex(8) + ".tmp")
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": VERSION, "port": self.server_address[1],
                           "token": self.token}, stream)
            os.replace(temporary, endpoint)
        finally:
            temporary.unlink(missing_ok=True)
        return endpoint

    def lm_stats(self) -> dict:
        return self.stats.report(self.worker.state if self.worker is not None else "off")


def run_resident(args) -> None:
    directory = args.runtime_dir if args.runtime_dir is not None else DEFAULT_RUNTIME_DIR
    private_directory(directory)
    with process_lock(directory / "server.lock") as acquired:
        if not acquired:
            return  # Another starter owns readiness, even while loading the lexicon.
        worker = None
        if args.lm_rescore:
            # The dictionary is enough to serve; the model loads on the worker
            # and conversions use the Viterbi 1-best until it is ready.
            loaded = load_session(args, with_lm=False)
            worker = LmWorker(loader=lambda: build_rescorer(args), lex=loaded._lex)
        else:
            loaded = load_session(args)
        gc.freeze()
        lm_options = {"grace_ms": args.lm_grace_ms, "deadline_ms": args.lm_deadline_ms,
                      "prefetch": args.lm_prefetch,
                      "prefetch_delay_ms": args.lm_prefetch_delay_ms}
        with ResidentServer(SessionFactory(loaded), log_text=args.log_text,
                            lm_timing=args.lm_timing, worker=worker,
                            lm_options=lm_options) as server:
            endpoint = server.publish(directory)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                endpoint.unlink(missing_ok=True)
