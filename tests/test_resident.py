from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import weakref

from oyayubi.ime import client, resident, server
from oyayubi.ime.lm_async import LmWorker
from oyayubi.ime.lm_rescore import LmRescorer
from oyayubi.ime.runtime_platform import process_lock, spawn_detached
from oyayubi.ime.session import ImeSession
from tools.wp0.mozc_dict import Entry, MozcLex
from tools.wp0.table import load_table


def loaded_session(learning_path=None):
    # A has plain=う. A confirmation of the initially second row must change the best.
    lex = MozcLex({"う": [Entry("宇", "う", 1, 1, 100), Entry("卯", "う", 1, 1, 200)]},
                  {"う"}, [0] * 4, 2)
    session = ImeSession(_table=load_table(), _lex=lex, learning_path=learning_path)
    session.load()
    return session


def type_a(connection, t=0):
    connection.request({"op": "down", "vk": 65, "t": t})
    return connection.request({"op": "up", "vk": 65, "t": t + 30})


class ResidentTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)

    @contextmanager
    def running(self, factory=None, **kwargs):
        with resident.ResidentServer(factory or resident.SessionFactory(loaded_session()),
                                     **kwargs) as instance:
            instance.publish(self.directory)
            thread = threading.Thread(target=instance.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            try:
                yield instance
            finally:
                instance.shutdown()
                thread.join(2)
                self.assertFalse(thread.is_alive())

    def connect(self):
        connection = client.connect(self.directory, timeout=1)
        self.addCleanup(connection.close)
        return connection

    def test_two_live_connections_isolate_fsm_composition_direct_and_context(self):
        sessions = []
        factory = resident.SessionFactory(loaded_session())

        def create():
            session = factory()
            sessions.append(session)
            return session

        with self.running(create):
            first, second = self.connect(), self.connect()
            barrier = threading.Barrier(2)

            def send(connection, vk):
                connection.request({"op": "down", "vk": vk, "t": 0})
                barrier.wait(timeout=2)  # Both have a pending chord on live connections.
                return connection.request({"op": "up", "vk": vk, "t": 30})

            with ThreadPoolExecutor(2) as pool:
                a = pool.submit(send, first, 65)
                j = pool.submit(send, second, 74)
                self.assertEqual(a.result(timeout=3)["composition"], "宇")
                self.assertEqual(j.result(timeout=3)["composition"], "と")
            first.request({"op": "context", "text": "一方の上文"})
            second.request({"op": "context", "text": "他方の上文"})
            self.assertEqual([s._lm_context for s in sessions], ["一方の上文", "他方の上文"])
            first.request({"op": "toggle_direct"})
            reply = second.request({"op": "timeout", "t": 100})
            self.assertEqual((reply["direct"], reply["composition"]), (False, "と"))
            self.assertTrue(first.request({"op": "down", "vk": 65})["pass_through"])
            second.request({"op": "reset"})
            self.assertEqual(sessions[1]._lm_context, "")
            self.assertEqual(sessions[0]._lm_context, "")  # direct transition also resets
            self.assertIs(sessions[0]._lex, sessions[1]._lex)
            self.assertIs(sessions[0]._table, sessions[1]._table)
            self.assertIsNot(sessions[0].fsm, sessions[1].fsm)

    def test_disconnect_discards_session_and_quit_only_closes_one_peer(self):
        refs = []
        factory = resident.SessionFactory(loaded_session())

        def create():
            session = factory()
            refs.append(weakref.ref(session))
            return session

        with self.running(create):
            first, second = self.connect(), self.connect()
            type_a(first)
            first.close()
            deadline = time.monotonic() + 2
            while refs[0]() is not None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNone(refs[0]())
            with self.assertRaises(EOFError):
                second.request({"op": "quit"})
            third = self.connect()
            reply = third.request({"op": "timeout", "t": 100})
            self.assertEqual((reply["composition"], reply["direct"], reply["timer"]),
                             ("", False, None))

    def test_shared_learning_cross_connection_and_session_scoped_undo(self):
        path = self.directory / "learning.jsonl"
        path.touch()
        loaded = loaded_session(path)
        with self.running(resident.SessionFactory(loaded)):
            first, second = self.connect(), self.connect()
            self.assertEqual(type_a(first)["composition"], "宇")
            first.request({"op": "down", "vk": 32, "t": 100})
            reply = first.request({"op": "up", "vk": 32, "t": 130})
            self.assertEqual(reply["candidates"][:2], ["宇", "卯"])
            first.request({"op": "down", "vk": ord("2"), "t": 200})
            reply = first.request({"op": "down", "vk": 13, "t": 250})
            self.assertEqual(reply["commit"], "卯")
            self.assertEqual(type_a(second)["composition"], "卯")
            second.request({"op": "undo_learning"})  # No confirmation in this session.
            self.assertEqual(len(path.read_text().splitlines()), 1)
            first.request({"op": "undo_learning"})
            second.request({"op": "reset"})
            self.assertEqual(type_a(second, 400)["composition"], "宇")
            path.unlink()
            second.request({"op": "down", "vk": 13, "t": 500})
            self.assertFalse(path.exists())
            self.assertFalse(loaded._learning.active)

    def test_shared_learning_serializes_simultaneous_commits(self):
        path = self.directory / "learning.jsonl"
        path.touch()
        loaded = loaded_session(path)
        with self.running(resident.SessionFactory(loaded)):
            first, second = self.connect(), self.connect()
            type_a(first)
            type_a(second)
            with ThreadPoolExecutor(2) as pool:
                replies = list(pool.map(lambda c: c.request({"op": "down", "vk": 13}),
                                        (first, second)))
            self.assertEqual([r["commit"] for r in replies], ["宇", "宇"])
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(len({r["record_id"] for r in rows}), 2)

    def test_lm_model_shared_but_context_and_timing_are_per_connection(self):
        calls = []

        class Scorer:
            def nll(self, context, candidates):
                calls.append(context)
                return [0] * len(candidates)

        loaded = loaded_session()
        loaded.lm = LmRescorer(Scorer())
        sessions = []
        factory = resident.SessionFactory(loaded)

        def create():
            session = factory()
            sessions.append(session)
            return session

        with self.running(create):
            first, second = self.connect(), self.connect()
            for connection, context in ((first, "一"), (second, "二")):
                connection.request({"op": "context", "text": context})
                type_a(connection)
                connection.request({"op": "down", "vk": 32, "t": 100})
                connection.request({"op": "up", "vk": 32, "t": 130})
            self.assertEqual(calls, ["一", "二"])
            self.assertIs(sessions[0].lm.scorer, sessions[1].lm.scorer)
            self.assertIsNot(sessions[0].lm, sessions[1].lm)
            self.assertIsNot(sessions[0].lm.last, sessions[1].lm.last)

    def test_authentication_and_protocol_errors_do_not_allocate_or_break_peers(self):
        factory = Mock(side_effect=resident.SessionFactory(loaded_session()))
        with self.running(factory) as instance:
            for hello in ({"op": "hello", "version": 1, "token": "wrong"},
                          {"op": "hello", "version": 2, "token": instance.token},
                          {"op": "hello", "version": 1, "token": "日本語"}):
                with socket.create_connection(instance.server_address, timeout=1) as sock:
                    sock.sendall((json.dumps(hello) + "\n").encode())
                    self.assertEqual(sock.recv(1024), b"")
            self.assertEqual(factory.call_count, 0)
            good, bad = self.connect(), self.connect()
            with self.assertRaises(EOFError):
                bad.request({"op": "down", "vk": "secret-invalid-int"})
            self.assertEqual(type_a(good)["composition"], "宇")

    def test_no_text_or_token_log_by_default_and_opt_in_text_logging(self):
        for log_text in (False, True):
            with self.subTest(log_text=log_text), patch.object(server.sys, "stderr", io.StringIO()) as log:
                with self.running(log_text=log_text) as instance:
                    with client.connect(self.directory, timeout=1) as connection:
                        type_a(connection)
                        connection.request({"op": "down", "vk": 13})
                    self.assertNotIn(instance.token, log.getvalue())
                    if log_text:
                        self.assertIn("宇", log.getvalue())
                    else:
                        self.assertEqual(log.getvalue(), "")

    def test_endpoint_permissions_and_secret_rotation(self):
        with self.running() as first:
            token = first.token
            if os.name != "nt":
                self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
                self.assertEqual((self.directory / "endpoint.json").stat().st_mode & 0o777, 0o600)
        with self.running() as second:
            self.assertNotEqual(second.token, token)
            self.assertEqual(self.connect().request({"op": "reset"})["composition"], "")

    def test_process_lock_contention_and_release(self):
        path = self.directory / "server.lock"
        with process_lock(path) as first:
            self.assertTrue(first)
            with process_lock(path) as second:
                self.assertFalse(second)
        with process_lock(path) as third:
            self.assertTrue(third)

    def test_startup_timeout_and_spawn_failure_are_bounded(self):
        child = Mock()
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            client.connect_or_start(self.directory, timeout=0.08, spawn=Mock(return_value=child))
        self.assertLess(time.monotonic() - start, 0.5)
        child.poll.assert_called_once()
        with self.assertRaises(OSError):
            client.connect_or_start(self.directory, spawn=Mock(side_effect=OSError("spawn failed")))

    def test_request_timeout_closes_connection_without_retry(self):
        entered, release = threading.Event(), threading.Event()
        real = server.dispatch

        def slow(sess, msg):
            entered.set()
            release.wait(2)
            return real(sess, msg)

        with self.running(), patch.object(server, "dispatch", side_effect=slow):
            connection = self.connect()
            connection.connection.settimeout(0.03)
            try:
                with self.assertRaises(TimeoutError):
                    type_a(connection)
                self.assertTrue(entered.is_set())
                self.assertTrue(connection.stream.closed)
            finally:
                release.set()


class SlowScorer:
    """Prefers 卯 over the Viterbi 宇; each call sleeps (a slow model)."""

    def __init__(self, sleep=0.2):
        self.sleep = sleep

    def nll(self, context, candidates):
        time.sleep(self.sleep)
        return [0.0 if c.startswith("卯") else 10.0 for c in candidates]


class AsyncLmResidentTest(unittest.TestCase):
    """Design §7 tests 7-8: the resident pipeline over TCP."""
    setUp = ResidentTest.setUp
    running = ResidentTest.running
    connect = ResidentTest.connect

    def worker(self, loader):
        return LmWorker(loader, loaded_session()._lex)

    def wait_state(self, worker, state):
        deadline = time.monotonic() + 5
        while worker.state != state and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(worker.state, state)

    def test_pending_reply_then_poll_then_replay(self):
        worker = self.worker(lambda: LmRescorer(SlowScorer()))
        self.wait_state(worker, "ready")
        with self.running(worker=worker) as instance:
            connection = self.connect()
            type_a(connection)
            connection.request({"op": "down", "vk": 32, "t": 100})
            start = time.monotonic()
            reply = connection.request({"op": "up", "vk": 32, "t": 130})
            self.assertLess(time.monotonic() - start, 0.1)
            self.assertEqual((reply["pending"], reply["converted"], reply["composition"],
                              reply["candidates"], reply["timer"]), (True, False, "宇", [], 140))
            t = 130
            while reply["pending"]:
                self.assertEqual(reply["timer"], t + 10)  # poll on the next timer tick
                time.sleep(0.016)
                t += 16
                reply = connection.request({"op": "timeout", "t": t})
            self.assertEqual((reply["converted"], reply["composition"]), (True, "卯"))
            self.assertEqual(reply["candidates"][reply["cand_index"]], "卯")
            # Enter commits it; the next conversion is typed into while pending.
            self.assertEqual(connection.request({"op": "down", "vk": 13, "t": 900})["commit"], "卯")
            type_a(connection, 1000)
            connection.request({"op": "down", "vk": 32, "t": 1100})
            self.assertTrue(connection.request({"op": "up", "vk": 32, "t": 1130})["pending"])
            reply = type_a(connection, 1140)  # A again: queued behind the conversion
            self.assertEqual((reply["pending"], reply["composition"], reply["commit"]),
                             (True, "宇", ""))
            t = 1200
            while reply["pending"]:
                time.sleep(0.016)
                reply = connection.request({"op": "timeout", "t": t})
                t += 16
            self.assertEqual((reply["commit"], reply["composition"], reply["converted"]),
                             ("卯", "宇", False))
            stats = connection.request({"op": "stats"})["stats"]
            self.assertEqual(stats["lm"], "ready")
            self.assertEqual(stats["conversions"]["pending"], 2)
            self.assertEqual(sum(stats["wait_ms"]["counts"]), 2)
            self.assertGreater(sum(stats["request_ms"]["counts"]), 0)

    def test_server_serves_while_loader_is_slow_or_fails(self):
        release = threading.Event()

        def slow():
            release.wait(5)
            return LmRescorer(SlowScorer(0))

        def broken():
            raise ImportError("no torch")

        for name, loader in (("slow", slow), ("broken", broken)):
            with self.subTest(loader=name), patch.object(server.sys, "stderr", io.StringIO()):
                worker = self.worker(loader)
                if name == "broken":
                    self.wait_state(worker, "disabled")
                with self.running(worker=worker):
                    connection = self.connect()
                    type_a(connection)
                    connection.request({"op": "down", "vk": 32, "t": 100})
                    reply = connection.request({"op": "up", "vk": 32, "t": 130})
                    self.assertEqual((reply["pending"], reply["converted"], reply["composition"]),
                                     (False, True, "宇"))
                    stats = connection.request({"op": "stats"})["stats"]
                    self.assertEqual(stats["lm"], "loading" if name == "slow" else "disabled")
                    self.assertEqual(stats["conversions"]["bypass"], 1)
                    if name == "slow":
                        release.set()
                        self.wait_state(worker, "ready")
                        connection.request({"op": "down", "vk": 27, "t": 200})  # back to 宇
                        connection.request({"op": "down", "vk": 32, "t": 300})
                        reply = connection.request({"op": "up", "vk": 32, "t": 330})
                        self.assertEqual(reply["composition"], "卯")

    def test_stats_without_worker_and_client_flag(self):
        with self.running():
            with patch.object(client.sys, "argv", ["client", "--runtime-dir", str(self.directory),
                                                    "--stats"]), \
                 patch.object(client.sys, "stdout", io.StringIO()) as out:
                client.main()
            self.assertEqual(json.loads(out.getvalue())["lm"], "off")


class LifecycleProcessTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.children = []
        self.addCleanup(self.stop_children)

    def spawn(self, command):
        command = list(command)
        command[command.index("oyayubi.ime.server")] = "tests.resident_fixture"
        child = spawn_detached(command)
        self.children.append(child)
        return child

    def stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)

    def connect(self, **kwargs):
        connection = client.connect_or_start(self.directory, timeout=5, request_timeout=1,
                                              spawn=self.spawn, **kwargs)
        self.addCleanup(connection.close)
        return connection

    def test_spawn_once_reuse_then_crash_recovery_from_stale_endpoint(self):
        first = self.connect()
        token = json.loads((self.directory / "endpoint.json").read_text())["token"]
        second = client.connect_or_start(self.directory, spawn=Mock(side_effect=AssertionError))
        self.addCleanup(second.close)
        self.assertEqual(len(self.children), 1)
        type_a(first)
        self.assertTrue(first.request({"op": "toggle_direct"})["direct"])
        self.children[0].kill()
        self.children[0].wait(timeout=5)
        with self.assertRaises((OSError, EOFError)):
            first.request({"op": "reset"})
        third = self.connect()
        reply = third.request({"op": "timeout", "t": 100})
        self.assertEqual((reply["direct"], reply["composition"], reply["timer"]), (False, "", None))
        self.assertNotEqual(json.loads((self.directory / "endpoint.json").read_text())["token"], token)
        self.assertEqual((self.directory / "loads").read_text().splitlines(), ["load", "load"])

    def test_concurrent_starters_load_dictionary_once(self):
        with ThreadPoolExecutor(3) as pool:
            connections = list(pool.map(lambda _: client.connect_or_start(
                self.directory, timeout=5, request_timeout=1, spawn=self.spawn,
                server_args=("--load-delay", "0.2")), range(3)))
        for connection in connections:
            self.addCleanup(connection.close)
            self.assertEqual(type_a(connection)["composition"], "う")
        self.assertEqual((self.directory / "loads").read_text().splitlines(), ["load"])
        # All duplicate subprocesses exit instead of loading a second dictionary.
        deadline = time.monotonic() + 2
        while sum(c.poll() is None for c in self.children) > 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(sum(c.poll() is None for c in self.children), 1)


if __name__ == "__main__":
    unittest.main()
