"""REDESIGN §5.7: the broker with a fake engine and a fake TIP, as processes.

POSIX: the broker is built here with CMake (src/broker) into build/broker. It
is the same Broker core with a POSIX platform layer: a Unix domain socket
stands for the named pipe, PR_SET_PDEATHSIG for the job object.
Windows: YURAGI_BROKER names the built yuragi-broker.exe; the TIP side is the
named pipe, and after every test no top-level window may belong to the broker
or the processes it started.

Every notice the user would get is also recorded in logs/notices.log. Ten
minutes are skipped with the test clock hook ({"op":"test_advance"}, accepted
only with --test-hooks).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "broker"
BUILD = ROOT / "build" / "broker"
FAKE_ENGINE = SOURCE / "tests" / "fake_engine.py"
_built = None


WINDOWS = sys.platform == "win32"


def broker_binary() -> Path:
    """Build the broker once per test run (CMake, Release); on Windows, YURAGI_BROKER."""
    global _built
    if _built is None and WINDOWS:
        if not os.environ.get("YURAGI_BROKER"):
            raise AssertionError("set YURAGI_BROKER to the built yuragi-broker.exe")
        _built = Path(os.environ["YURAGI_BROKER"])
    if _built is None:
        subprocess.run(["cmake", "-S", str(SOURCE), "-B", str(BUILD),
                        "-DCMAKE_BUILD_TYPE=Release"], check=True, capture_output=True)
        subprocess.run(["cmake", "--build", str(BUILD), "-j", "4"], check=True,
                       capture_output=True)
        _built = BUILD / "yuragi-broker"
    return _built


def alive(pid: int) -> bool:
    """The process exists and has not exited (a zombie counts as gone)."""
    if WINDOWS:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with open(f"/proc/{pid}/stat") as stat:
        return stat.read().split()[2] != "Z"


def top_level_windows(pids) -> list[str]:
    """Visible top-level windows owned by these processes (Windows only)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []
    wanted = set(pids)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in wanted and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            found.append(f"{pid.value}: {title.value}")
        return True
    user32.EnumWindows(visit, 0)
    return found


def wait_until(predicate, timeout=20.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("timed out waiting")


class Connection:
    """The TIP's end: a Unix domain socket, or on Windows the named pipe.

    A synchronous pipe handle runs one operation at a time, so on Windows the
    reader only reads what PeekNamedPipe says is there (a blocked read would
    hold up every write)."""

    def __init__(self, address: str):
        if WINDOWS:
            import _winapi
            import msvcrt
            self.file = open(address, "r+b", buffering=0)
            self.handle = msvcrt.get_osfhandle(self.file.fileno())
            self.peek = _winapi.PeekNamedPipe
        else:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(address)

    def recv(self) -> bytes:
        if not WINDOWS:
            try:
                return self.sock.recv(65536)
            except OSError:
                return b""
        while True:
            try:
                available = self.peek(self.handle)[0]
                if available:
                    return self.file.read(available)
            except OSError:
                return b""
            time.sleep(0.005)

    def send(self, data: bytes) -> None:
        if WINDOWS:
            self.file.write(data)
        else:
            self.sock.sendall(data)

    def close(self) -> None:
        (self.file if WINDOWS else self.sock).close()


class FakeTip:
    """A TIP: one connection, one line of JSON per message."""

    def __init__(self, address: str):
        self.connection = Connection(address)
        self.lines: queue.Queue = queue.Queue()
        self.seen: list[dict] = []
        self.state = None  # as last told: the hello reply or a state event
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buffer = b""
        while True:
            chunk = self.connection.recv()
            if not chunk:
                self.lines.put(None)
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                self.lines.put(json.loads(line))

    def send(self, msg: dict) -> None:
        self.connection.send((json.dumps(msg) + "\n").encode("utf-8"))

    def next(self, timeout=20.0):
        line = self.lines.get(timeout=timeout)
        if line is not None:
            self.seen.append(line)
            if line.get("event") == "state" or "proto" in line:
                self.state = line["state"]
        return line

    def wait_for(self, predicate, timeout=20.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            line = self.next(max(0.01, deadline - time.monotonic()))
            if line is None:
                raise AssertionError("the broker closed the connection")
            if predicate(line):
                return line

    def hello(self) -> dict:
        self.send({"op": "hello", "proto": 1, "tag": "test", "bits": 64})
        return self.next()

    def wait_state(self, state: str, timeout=20.0) -> None:
        """Until the broker has said the state is this (now or before)."""
        if self.state != state:
            self.wait_for(lambda m: self.state == state, timeout)

    def key(self, seq: int, vk: int = 74, op: str = "down") -> None:
        self.send({"seq": seq, "op": op, "vk": vk, "t": seq * 100, "mods": []})

    def reply(self, seq: int, timeout=20.0) -> dict:
        return self.wait_for(lambda m: m.get("seq") == seq, timeout)

    def close(self):
        self.connection.close()


class BrokerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.data = self.dir / "data"
        self.state = self.dir / "engine-state"
        self.state.mkdir()
        if WINDOWS:
            self.address = rf"\\.\pipe\yuragi-ime-test-{os.getpid()}-{id(self)}"
        else:
            self.address = str(self.dir / "broker.sock")
        self.engine_json = self.dir / "engine.json"
        self.tips: list[FakeTip] = []

    def plan(self, *modes) -> None:
        (self.state / "plan.json").write_text(json.dumps(list(modes)), encoding="utf-8")
        self.write_engine_json([str(FAKE_ENGINE), str(self.state)])

    def write_engine_json(self, args, app=None, python=sys.executable, pythonw=None):
        self.engine_json.write_text(json.dumps({
            "format": 1, "python": python, "pythonw": pythonw or python,
            "app": str(app or self.dir), "args": args,
            "admin_args": [str(self.dir / "admin.py")]}), encoding="utf-8")

    def start(self, *extra, engine_json=True) -> subprocess.Popen:
        argv = [str(broker_binary()), "--data-dir", str(self.data),
                "--pipe" if WINDOWS else "--socket", self.address, "--test-hooks", *extra]
        if engine_json:
            argv += ["--engine-json", str(self.engine_json)]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, proc)
        return proc

    def stop(self, proc):
        if WINDOWS:
            # §5.7: across every test, no window from the broker or its children.
            self.assertEqual(top_level_windows([proc.pid, *self.engine_pids()]), [])
        for tip in self.tips:
            tip.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for stream in (proc.stdout, proc.stderr):
            stream.close()
        for pid in self.engine_pids():
            if alive(pid):
                os.kill(pid, signal.SIGTERM if WINDOWS else signal.SIGKILL)

    def tip(self) -> FakeTip:
        def connect():
            try:
                return FakeTip(self.address)
            except OSError:  # the broker is not listening yet
                return None
        tip = wait_until(connect)
        self.tips.append(tip)
        return tip

    def engine_pids(self) -> list[int]:
        starts = self.state / "starts"
        return [int(p) for p in starts.read_text().split()] if starts.exists() else []

    def starts(self) -> int:
        return len(self.engine_pids())

    def received(self, start: int) -> list[dict]:
        path = self.state / f"received-{start}.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def notices(self) -> list[dict]:
        """What the user was told (logs/notices.log), without the times."""
        path = self.data / "logs" / "notices.log"
        lines = path.read_text().splitlines() if path.exists() else []
        return [{k: v for k, v in json.loads(l).items() if k != "time"} for l in lines]

    def log(self) -> str:
        path = self.data / "logs" / "broker.log"
        return path.read_text() if path.exists() else ""


class SupervisionTest(BrokerCase):
    def test_immediate_crash_starts_exactly_twice_then_nothing_for_ten_minutes(self):
        self.plan("crash")
        self.start()
        tip = self.tip()
        tip.hello()
        tip.wait_state("down")
        self.assertEqual(self.starts(), 2)
        self.assertEqual(self.notices(), [{"stopped": "repeated"}])
        tip.send({"op": "test_advance", "ms": 600_000})  # ten minutes on the broker's clock
        tip.key(1)
        self.assertEqual(tip.reply(1), {"seq": 1, "state": "down"})
        time.sleep(1.0)
        self.assertEqual(self.starts(), 2)
        self.assertEqual(len(self.notices()), 1)

    def test_fatal_exit_code_is_the_reason(self):
        self.plan({"exit": 21})
        self.start()
        tip = self.tip()
        tip.hello()
        tip.wait_state("down")
        self.assertEqual(self.starts(), 2)
        self.assertEqual(self.notices(), [{"stopped": "dictionary"}])
        self.assertIn("fatal 21 test", self.log())
        engine_logs = self.data / "logs"
        self.assertIn("fatal 21 test", (engine_logs / "engine.log").read_text())
        self.assertIn("fatal 21 test", (engine_logs / "engine.prev.log").read_text())

    def test_crash_at_the_nth_request_hands_held_keys_to_the_next_engine_in_order(self):
        self.plan({"crash_after": 3}, "normal")
        self.start()
        tip = self.tip()
        tip.hello()
        tip.wait_state("ready")
        tip.send({"seq": 1, "op": "set_direct", "direct": True})
        self.assertTrue(tip.reply(1)["direct"])
        tip.key(2)
        self.assertEqual(tip.reply(2)["composition"], "down:74")
        tip.key(3)  # the engine dies on this one
        self.assertEqual(tip.reply(3), {"seq": 3, "state": "down"})
        for seq in (4, 5, 6):  # typed while it starts again
            tip.key(seq, vk=60 + seq)
        replies = [tip.reply(seq) for seq in (4, 5, 6)]
        self.assertEqual([r["composition"] for r in replies], ["down:64", "down:65", "down:66"])
        self.assertTrue(all(r["direct"] for r in replies))  # the input mode came back
        resets = [m for m in tip.seen if m.get("event") == "reset"]
        self.assertEqual(len(resets), 1)
        reset_at = tip.seen.index(resets[0])
        self.assertLess(reset_at, tip.seen.index(replies[0]))  # reset before the held keys
        new = self.received(1)
        self.assertEqual(new[0], {"op": "open", "sid": new[0]["sid"]})
        self.assertEqual(new[1]["op"], "set_direct")
        self.assertEqual([m.get("seq") for m in new[2:]], [4, 5, 6])
        self.assertEqual(self.starts(), 2)

    def test_engine_that_does_not_answer_is_ended_after_five_seconds(self):
        self.plan("silent", "normal")
        self.start()
        tip = self.tip()
        tip.hello()
        tip.wait_state("ready")
        sent = time.monotonic()
        tip.key(1)
        self.assertEqual(tip.reply(1, timeout=15), {"seq": 1, "state": "down"})
        waited = time.monotonic() - sent
        self.assertGreaterEqual(waited, 4.9)
        self.assertLess(waited, 8)
        tip.wait_state("starting")
        tip.wait_state("ready")
        self.assertEqual(self.starts(), 2)
        tip.key(2)
        self.assertEqual(tip.reply(2)["composition"], "down:74")

    def test_three_crashes_stop_and_retry_starts_exactly_once(self):
        self.plan({"crash_after": 1}, {"crash_after": 1}, {"crash_after": 1}, "normal")
        self.start()
        tip = self.tip()
        tip.hello()
        for seq in (1, 2, 3):
            tip.wait_state("ready", timeout=30)
            tip.key(seq)
            self.assertEqual(tip.reply(seq), {"seq": seq, "state": "down"})
            tip.wait_state("starting" if seq < 3 else "down", timeout=30)
        self.assertEqual(self.starts(), 3)
        self.assertEqual(self.notices(), [{"stopped": "repeated"}])
        tip.send({"op": "retry"})
        tip.wait_state("ready")
        time.sleep(1.0)
        self.assertEqual(self.starts(), 4)
        tip.key(9)
        self.assertEqual(tip.reply(9)["composition"], "down:74")

    def test_keys_held_too_long_are_answered_down(self):
        self.plan("no_ready")
        self.start()
        tip = self.tip()
        self.assertEqual(tip.hello()["state"], "starting")
        tip.key(1)
        tip.key(2, op="up")
        tip.send({"op": "test_advance", "ms": 8000})
        self.assertEqual(tip.reply(1), {"seq": 1, "state": "down"})
        self.assertEqual(tip.reply(2), {"seq": 2, "state": "down"})

    def test_warnings_are_told_once_and_given_to_new_tips(self):
        self.plan({"warnings": ["config"]})
        self.start()
        tip = self.tip()
        tip.hello()
        tip.wait_state("ready")
        self.assertEqual(self.notices(), [{"warning": "config"}])
        other = self.tip()
        self.assertEqual(other.hello(), {"proto": 1, "state": "ready", "warnings": ["config"]})


class ProcessTest(BrokerCase):
    def test_killing_the_broker_ends_the_engine(self):
        self.plan("normal")
        proc = self.start()
        tip = self.tip()
        tip.hello()
        tip.wait_state("ready")
        engine = self.engine_pids()[0]
        proc.kill()
        proc.wait()
        wait_until(lambda: not alive(engine), timeout=5)

    def test_one_broker_per_user(self):
        self.plan("normal")
        self.start()
        self.tip().hello()
        other = self.address + "-other"
        second = subprocess.run([str(broker_binary()), "--data-dir", str(self.data),
                                 "--pipe" if WINDOWS else "--socket", other,
                                 "--engine-json", str(self.engine_json)], timeout=10)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(self.starts(), 1)

    def test_not_installed_starts_nothing_and_says_so(self):
        self.start(engine_json=False)
        tip = self.tip()
        self.assertEqual(tip.hello()["state"], "not-installed")
        tip.key(1)
        self.assertEqual(tip.reply(1), {"seq": 1, "state": "not-installed"})
        self.assertEqual(self.starts(), 0)

    def test_maintenance_lock_keeps_the_engine_from_starting(self):
        self.plan("normal")
        self.data.mkdir(parents=True)
        if WINDOWS:  # §8.1: Local\yuragi-ime-maintenance, as setup.ps1 holds it
            import ctypes
            mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "Local\\yuragi-ime-maintenance")
            self.addCleanup(ctypes.windll.kernel32.CloseHandle, mutex)
        else:
            import fcntl
            lock = open(self.data / "maintenance.lock", "w")
            self.addCleanup(lock.close)
            fcntl.flock(lock, fcntl.LOCK_EX)
        proc = self.start()
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual(self.starts(), 0)
        self.assertIn("maintenance", self.log())

    def test_other_protocol_hears_incompatible_only(self):
        self.plan("normal")
        self.start()
        tip = self.tip()
        tip.send({"op": "hello", "proto": 2, "tag": "v9", "bits": 64})
        self.assertEqual(tip.next(), {"proto": 1, "state": "incompatible"})
        tip.key(1)
        self.assertEqual(tip.reply(1), {"seq": 1, "state": "incompatible"})

    def test_open_admin_runs_the_admin_command(self):
        marker = self.dir / "admin-ran"
        (self.dir / "admin.py").write_text(
            f"open({str(marker)!r}, 'w').write('yes')\n", encoding="utf-8")
        self.plan("normal")
        self.start()
        tip = self.tip()
        tip.hello()
        tip.send({"op": "open_admin"})
        wait_until(marker.exists, timeout=10)

    @unittest.skipUnless(WINDOWS, "the named pipe")
    def test_management_tools_name_the_real_pipe(self):
        # It once raised (ERROR_INVALID_HANDLE) and the window showed "not running"
        # beside a running broker.
        from yuragi.ime import broker_client
        sid = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                             text=True, check=True).stdout.strip().split(",")[-1].strip('"')
        prefix = rf"\\.\pipe\yuragi-ime-{sid}-"
        address = broker_client.pipe_address()
        self.assertTrue(address.startswith(prefix), address)
        self.assertTrue(address[len(prefix):].isdigit(), address)

    @unittest.skipIf(WINDOWS, "the management tools find the real pipe by the user's SID")
    def test_management_tools_reach_the_broker(self):
        from yuragi.ime import broker_client
        self.assertIsNone(broker_client.status(self.address))  # nothing listening yet
        self.plan({"warnings": ["lm"]}, "normal")
        self.start()
        tip = self.tip()
        tip.hello()
        tip.wait_state("ready")
        self.assertEqual(broker_client.status(self.address), {"state": "ready", "warnings": ["lm"]})
        self.assertTrue(broker_client.retry(self.address))  # §9.4: restart with new settings
        tip.wait_state("starting")
        tip.wait_state("ready")
        self.assertEqual(self.starts(), 2)

    def test_quit_ends_broker_and_engine(self):
        self.plan("normal")
        proc = self.start()
        tip = self.tip()
        tip.hello()
        tip.wait_state("ready")
        tip.send({"op": "quit"})
        self.assertEqual(proc.wait(timeout=10), 0)


class SelfTestTest(BrokerCase):
    def selftest(self) -> subprocess.CompletedProcess:
        return subprocess.run([str(broker_binary()), "--data-dir", str(self.data),
                               "--selftest", str(self.engine_json)],
                              capture_output=True, text=True, timeout=600)

    def test_real_engine_converts(self):
        """§10 stage 2: --selftest with the real engine (no LM)."""
        data = self.dir / "user"
        data.mkdir()
        (data / "config.json").write_text('{"lm_rescore": false}', encoding="utf-8")
        self.write_engine_json(["-m", "yuragi.engine", "--data-dir", str(data)], app=ROOT)
        result = self.selftest()
        self.assertEqual(result.returncode, 0, result.stdout + self.log())
        self.assertIn("selftest passed", result.stdout)
        self.assertIn("selftest: converted", self.log())

    def test_real_engine_through_the_installers_bootstrap(self):
        """engine.json as setup.ps1 writes it: "-c <$Boot> yuragi.engine ...", with
        Python isolated (-I) as the bundled runtime's ._pth makes it."""
        from tests.test_installer_scripts import boot_string
        from yuragi.lattice import mozc_dict
        data = self.dir / "user data"
        data.mkdir()
        (data / "config.json").write_text('{"lm_rescore": false}', encoding="utf-8")
        self.write_engine_json(["-I", "-c", boot_string(), "yuragi.engine", "--data-dir", str(data),
                                "--dict-dir", str(mozc_dict.DICT_DIR),
                                "--cache-dir", str(mozc_dict.CACHE.parent)], app=ROOT)
        result = self.selftest()
        self.assertEqual(result.returncode, 0, result.stdout + self.log())

    def test_engine_that_cannot_start_fails_the_selftest(self):
        self.plan({"exit": 21})
        result = self.selftest()
        self.assertEqual(result.returncode, 1)
        self.assertIn("fatal 21 test", self.log())


if __name__ == "__main__":
    unittest.main()
