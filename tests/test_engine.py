"""REDESIGN §6 / §10 stage 1: the engine process, run as the broker runs it.

Each test starts ``python -m oyayubi.engine`` with pipes and the broker's
environment, and talks the line protocol. The real dictionary is used (as for
the other session tests); the LM is used when torch and a local model are
present (OYAYUBI_LM_MODEL=<model directory>), otherwise the engine reports the
"lm" warning and converts without it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from oyayubi.lattice import mozc_dict

ROOT = Path(__file__).resolve().parents[1]
START_TIMEOUT = 300  # a cache rebuild on a slow machine
VK_KA = 0x57  # NICOLA-A plain face か


def broker_environment(extra=None) -> dict:
    """As the broker builds it (§5.3): the user's environment, plus four, minus two."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1",
               HF_HUB_OFFLINE="1")
    env.update(extra or {})
    return env


class EngineProcess:
    def __init__(self, data_dir: Path, *args, command=None, env=None, cwd=ROOT):
        command = command or [sys.executable, "-m", "oyayubi.engine"]
        self.proc = subprocess.Popen(
            [*command, "--data-dir", str(data_dir), *map(str, args)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=broker_environment(env))
        self.lines: queue.Queue = queue.Queue()
        self.stderr: list[str] = []
        self.seq = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self):
        for raw in self.proc.stdout:
            self.lines.put(json.loads(raw))  # every stdout line must be protocol JSON
        self.lines.put(None)

    def _read_err(self):
        for raw in self.proc.stderr:
            self.stderr.append(raw.decode("utf-8", "replace"))

    def next(self, timeout=30):
        return self.lines.get(timeout=timeout)

    def wait_ready(self) -> tuple[dict, list[dict]]:
        """The ready line, and what came before it."""
        before = []
        while True:
            line = self.next(START_TIMEOUT)
            if line is None:
                raise AssertionError("engine exited before ready:\n" + self.log())
            if line.get("ready"):
                return line, before
            before.append(line)

    def send(self, msg: dict) -> None:
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, sid: int, msg: dict) -> dict:
        self.seq += 1
        self.send({"sid": sid, "seq": self.seq, **msg})
        while True:
            reply = self.next()
            if reply is None:
                raise AssertionError("engine exited:\n" + self.log())
            if "seq" in reply:
                assert (reply["sid"], reply["seq"]) == (sid, self.seq), reply
                return reply

    def tap(self, sid: int, vk: int, t: int) -> dict:
        self.request(sid, {"op": "down", "vk": vk, "t": t})
        return self.request(sid, {"op": "up", "vk": vk, "t": t + 20})

    def close(self, timeout=60) -> int:
        self.proc.stdin.close()
        code = self.proc.wait(timeout)
        return code

    def log(self) -> str:
        time.sleep(0.2)
        return "".join(self.stderr)

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass  # stdin of a process that already exited


class EngineCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.data = self.tmp / "data"
        self.data.mkdir()

    def start(self, *args, **kwargs) -> EngineProcess:
        engine = EngineProcess(self.data, *args, **kwargs)
        self.addCleanup(engine.kill)
        return engine

    def write_config(self, values: dict) -> None:
        model = os.environ.get("OYAYUBI_LM_MODEL")
        if model:
            values = {"lm_model": model, **values}
        (self.data / "config.json").write_text(json.dumps(values), encoding="utf-8")


class BrokenUserFilesTest(EngineCase):
    """§6.2: the user's own files never keep the engine from starting."""

    def test_broken_files_reach_ready_and_requests_are_contained(self):
        (self.data / "config.json").write_text('{"lm_k": 4,, broken', encoding="utf-8")
        (self.data / "personal_lexicon.jsonl").write_text(
            '{"reading": "おやゆび", "surface": "親指"}\n{"reading": \n', encoding="utf-8")
        (self.data / "learning.jsonl").write_text("not json\n", encoding="utf-8")
        engine = self.start("--test-hooks")
        ready, _ = engine.wait_ready()
        self.assertEqual(ready["proto"], 1)
        self.assertIn("config", ready["warnings"])
        log = engine.log()
        self.assertIn("personal lexicon: 1 line(s) skipped", log)
        self.assertIn("1 lines, 0 live, 1 skipped", log)

        engine.send({"op": "open", "sid": 1})
        self.assertEqual(engine.tap(1, VK_KA, 0)["composition"], "か")
        # §6.4: a request that raises answers error:true from a clean session ...
        reply = engine.request(1, {"op": "test_raise"})
        self.assertTrue(reply["error"])
        self.assertEqual((reply["composition"], reply["n_tokens"]), ("", 0))
        self.assertIn("request failed: RuntimeError at engine.py", engine.log())
        # ... and the next request is served normally.
        reply = engine.tap(1, VK_KA, 1000)
        self.assertNotIn("error", reply)
        self.assertEqual(reply["composition"], "か")

        # §6.5: the 201st character changes nothing and says so.
        engine.send({"op": "open", "sid": 2})
        t = 0
        for i in range(200):
            reply = engine.tap(2, VK_KA, t)
            t += 300
            self.assertNotIn("full", reply)
        self.assertEqual(reply["n_tokens"], 200)
        before = reply["composition"]
        reply = engine.tap(2, VK_KA, t)
        self.assertTrue(reply["full"])
        self.assertEqual((reply["n_tokens"], reply["composition"]), (200, before))
        reply = engine.request(2, {"op": "down", "vk": 0x0D, "t": t + 400})  # Enter commits
        self.assertEqual(reply["commit"], before)
        self.assertNotIn("full", reply)
        self.assertEqual(engine.close(), 0)


class CacheTest(EngineCase):
    def test_zero_byte_cache_is_rebuilt_with_progress(self):
        cache = self.tmp / "cache"
        cache.mkdir()
        (cache / mozc_dict.CACHE.name).write_bytes(b"")
        engine = self.start("--cache-dir", cache)
        ready, before = engine.wait_ready()
        self.assertIn({"progress": "cache"}, before)
        self.assertGreater((cache / mozc_dict.CACHE.name).stat().st_size, 1_000_000)
        self.assertEqual(list(cache.glob("*.tmp")), [])
        self.assertIn("dictionary cache unreadable", engine.log())
        engine.send({"op": "open", "sid": 1})
        self.assertEqual(engine.tap(1, VK_KA, 0)["composition"], "か")
        self.assertEqual(engine.close(), 0)


class DictionaryTest(EngineCase):
    """§6.2/§6.6: the program's own files stop the engine, with a reason."""

    def dictionary_copy(self) -> Path:
        copy = self.tmp / "dict"
        copy.mkdir()
        for path in mozc_dict.DICT_DIR.iterdir():
            try:
                os.link(path, copy / path.name)
            except OSError:
                shutil.copy2(path, copy / path.name)
        return copy

    def test_a_missing_dictionary_file_exits_21(self):
        copy = self.dictionary_copy()
        (copy / "dictionary03.txt").unlink()
        engine = self.start("--dict-dir", copy)
        self.assertIsNone(engine.next(60))  # no ready, no output at all
        self.assertEqual(engine.proc.wait(60), 21)
        log = engine.log()
        self.assertIn("fatal 21 dictionary-missing", log)
        self.assertIn("missing dictionary03.txt", log)

    def test_a_changed_dictionary_file_exits_21(self):
        copy = self.dictionary_copy()
        target = copy / "suffix.txt"
        data = target.read_bytes()
        target.unlink()  # a hard link: do not write through to the real file
        target.write_bytes(data.replace(b"\t", b" ", 1))
        engine = self.start("--dict-dir", copy)
        self.assertEqual(engine.proc.wait(60), 21)
        self.assertIn("fatal 21 dictionary-changed", engine.log())

    def test_unreadable_program_files_exit_20(self):
        program = self.tmp / "program"
        shutil.copytree(ROOT / "oyayubi", program / "oyayubi",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (program / "data").mkdir()
        # The dictionary manifest is a program file; without it nothing can start.
        engine = self.start("--dict-dir", mozc_dict.DICT_DIR, cwd=program)
        self.assertEqual(engine.proc.wait(60), 20)
        self.assertIn("fatal 20 program-files-unreadable", engine.log())


AUDIT_RUNNER = r"""
import os, runpy, sys
fd = os.open(sys.argv.pop(1), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
def hook(event, args):
    if event.startswith("socket.") or event in ("urllib.Request", "http.client.connect"):
        detail = repr(args[1]) if event in ("socket.bind", "socket.connect") else ""
        os.write(fd, (event + " " + detail + "\n").encode())
sys.addaudithook(hook)
sys.argv[0] = "oyayubi.engine"
runpy.run_module("oyayubi.engine", run_name="__main__", alter_sys=True)
"""

SHIM = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <unistd.h>
static void note(const char *what, int family) {
    const char *path = getenv("OYAYUBI_NET_LOG");
    if (!path) return;
    int fd = open(path, O_WRONLY | O_APPEND | O_CREAT, 0600);
    if (fd < 0) return;
    char buf[64];
    int n = snprintf(buf, sizeof buf, "%s %d\n", what, family);
    if (n > 0) { ssize_t w = write(fd, buf, (size_t)n); (void)w; }
    close(fd);
}
ssize_t sendto(int fd, const void *buf, size_t len, int flags,
               const struct sockaddr *addr, socklen_t alen) {
    static ssize_t (*real)(int, const void *, size_t, int, const struct sockaddr *, socklen_t);
    if (!real) real = (ssize_t (*)(int, const void *, size_t, int, const struct sockaddr *,
                                   socklen_t))dlsym(RTLD_NEXT, "sendto");
    if (addr) note("sendto", addr->sa_family);
    return real(fd, buf, len, flags, addr, alen);
}
int connect(int fd, const struct sockaddr *addr, socklen_t len) {
    static int (*real)(int, const struct sockaddr *, socklen_t);
    if (!real) real = (int (*)(int, const struct sockaddr *, socklen_t))dlsym(RTLD_NEXT, "connect");
    note("connect", addr ? addr->sa_family : -1);
    return real(fd, addr, len);
}
"""


# What counts as opening a connection: connecting, resolving a name, sending.
CONNECTING = ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
              "socket.gethostbyaddr", "socket.sendto", "socket.sendmsg",
              "urllib.Request", "http.client.connect", "connect ", "sendto ")


class NetworkTest(EngineCase):
    """§6.2/§10: from start to exit, the engine opens no network connection.

    Python's audit hooks see every socket the interpreter makes; on Linux a
    preloaded shim also sees connect/sendto made by native libraries (torch, ...).
    Other socket events are printed, not failed: with the LM, importing
    transformers imports urllib3, which probes for IPv6 by binding a socket to
    ::1 port 0 and closing it (no connection, no traffic).
    """

    def test_no_socket_from_start_to_exit(self):
        self.write_config({})
        events = self.tmp / "audit.log"
        native = self.tmp / "native.log"
        env = {}
        shim = self.build_shim()
        if shim is not None:
            env.update(LD_PRELOAD=str(shim), OYAYUBI_NET_LOG=str(native))
        engine = self.start(command=[sys.executable, "-c", AUDIT_RUNNER, str(events)], env=env)
        engine.wait_ready()
        engine.send({"op": "open", "sid": 1})
        t = 0
        for vk in (0x57, 0x4C, 0x48, 0x4F):  # かいはつ
            engine.tap(1, vk, t)
            t += 300
        self.wait_for_lm(engine)
        reply = engine.tap(1, 0x20, t)  # convert (with the LM when it loaded)
        while reply["pending"]:
            t += 50
            reply = engine.request(1, {"op": "timeout", "t": t})
        self.assertTrue(reply["converted"])
        self.assertEqual(engine.close(), 0)
        log = engine.log()
        seen = (events.read_text() if events.exists() else "").splitlines()
        if shim is not None and native.exists():
            seen += native.read_text().splitlines()
        connecting = [e for e in seen if e.startswith(CONNECTING)]
        self.assertEqual(connecting, [], log)
        if seen:
            print("\nsocket events without a connection: " + "; ".join(seen), file=sys.stderr)
        if shim is None:
            print("\nnative socket shim not built (no Linux C compiler); "
                  "audit hooks only", file=sys.stderr)

    def wait_for_lm(self, engine, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            log = engine.log()
            if "LM loaded" in log or "LM rescoring not used" in log:
                print("\n" + ("LM loaded" if "LM loaded" in log else "no LM") + " in the network test",
                      file=sys.stderr)
                return
            time.sleep(0.5)
        self.fail("the LM neither loaded nor failed:\n" + engine.log())

    def build_shim(self) -> Path | None:
        compiler = shutil.which("cc") or shutil.which("gcc")
        if not sys.platform.startswith("linux") or compiler is None:
            return None
        source = self.tmp / "shim.c"
        source.write_text(SHIM, encoding="ascii")
        out = self.tmp / "shim.so"
        subprocess.run([compiler, "-shared", "-fPIC", "-o", str(out), str(source), "-ldl"],
                       check=True)
        return out


if __name__ == "__main__":
    unittest.main()
