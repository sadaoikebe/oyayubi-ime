"""The engine's sessions, in process (what tests/test_resident.py checked for the
resident server it replaces, REDESIGN §6.1 and §10 stage 5).

One engine serves every TIP connection: the dictionary, the face table, the
learning history and the word list are loaded once and shared; each sid has
its own session (chord FSM, composition, input mode, left context) and, with
the LM, its own pipeline.
"""
from __future__ import annotations

from argparse import Namespace
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import weakref

from oyayubi.engine import Engine
from oyayubi.ime.lm_async import LmWorker
from oyayubi.ime.lm_rescore import LmRescorer
from oyayubi.ime.session import ImeSession
from oyayubi.lattice.mozc_dict import Entry, MozcLex
from oyayubi.lattice.table import load_table

SETTINGS = Namespace(log_text=False, lm_timing=False, lm_grace_ms=30, lm_deadline_ms=400,
                     lm_prefetch="thumb", lm_prefetch_delay_ms=30)


def loaded_session(learning_path=None):
    # A has plain=う. A confirmation of the initially second row must change the best.
    lex = MozcLex({"う": [Entry("宇", "う", 1, 1, 100), Entry("卯", "う", 1, 1, 200)]},
                  {"う"}, [0] * 4, 2)
    session = ImeSession(_table=load_table(), _lex=lex, learning_path=learning_path)
    session.load()
    return session


class Channel:
    def __init__(self):
        self.warnings = []

    def warning(self, code):
        self.warnings.append(code)

    def send(self, message):
        raise AssertionError("requests are answered by Engine.request in these tests")


class EngineCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.seq = 0

    def engine(self, loaded=None, worker=None, settings=SETTINGS):
        engine = Engine(Channel(), Namespace(test_hooks=False))
        engine.loaded = loaded or loaded_session()
        engine.worker = worker
        engine.settings = settings
        engine.log_text = settings.log_text
        return engine

    def open(self, engine, sid):
        engine.serve([json.dumps({"op": "open", "sid": sid}).encode()])

    def request(self, engine, sid, msg):
        self.seq += 1
        reply = engine.request({"sid": sid, "seq": self.seq, **msg})
        self.assertEqual((reply["sid"], reply["seq"]), (sid, self.seq))
        return reply

    def type_a(self, engine, sid, t=0):
        self.request(engine, sid, {"op": "down", "vk": 65, "t": t})
        return self.request(engine, sid, {"op": "up", "vk": 65, "t": t + 30})


class SessionsTest(EngineCase):
    def test_sessions_are_isolated_and_share_the_loaded_data(self):
        engine = self.engine()
        self.open(engine, 1)
        self.open(engine, 2)
        self.request(engine, 1, {"op": "down", "vk": 65, "t": 0})
        self.request(engine, 2, {"op": "down", "vk": 74, "t": 0})  # both mid-chord
        self.assertEqual(self.request(engine, 1, {"op": "up", "vk": 65, "t": 30})["composition"], "宇")
        self.assertEqual(self.request(engine, 2, {"op": "up", "vk": 74, "t": 30})["composition"], "と")
        self.request(engine, 1, {"op": "context", "text": "一方の上文"})
        self.request(engine, 2, {"op": "context", "text": "他方の上文"})
        first, second = engine.connections[1].sess, engine.connections[2].sess
        self.assertEqual((first._lm_context, second._lm_context), ("一方の上文", "他方の上文"))
        self.request(engine, 1, {"op": "toggle_direct"})
        reply = self.request(engine, 2, {"op": "timeout", "t": 100})
        self.assertEqual((reply["direct"], reply["composition"]), (False, "と"))
        self.assertTrue(self.request(engine, 1, {"op": "down", "vk": 65})["pass_through"])
        self.assertIs(first._lex, second._lex)
        self.assertIs(first._table, second._table)
        self.assertIsNot(first.fsm, second.fsm)

    def test_close_discards_the_session_and_others_go_on(self):
        engine = self.engine()
        self.open(engine, 1)
        self.open(engine, 2)
        ref = weakref.ref(engine.connections[1].sess)
        self.type_a(engine, 1)
        engine.serve([b'{"op":"close","sid":1}'])
        self.assertIsNone(ref())
        with redirect_stderr(io.StringIO()):
            self.assertTrue(self.request(engine, 1, {"op": "reset"})["error"])  # not open
        self.assertEqual(self.type_a(engine, 2)["composition"], "宇")

    def test_shared_learning_and_session_scoped_undo(self):
        path = self.dir / "learning.jsonl"
        path.touch()
        loaded = loaded_session(path)
        engine = self.engine(loaded)
        self.open(engine, 1)
        self.open(engine, 2)
        self.assertEqual(self.type_a(engine, 1)["composition"], "宇")
        self.request(engine, 1, {"op": "down", "vk": 32, "t": 100})
        reply = self.request(engine, 1, {"op": "up", "vk": 32, "t": 130})
        self.assertEqual(reply["candidates"][:2], ["宇", "卯"])
        self.request(engine, 1, {"op": "down", "vk": ord("2"), "t": 200})
        self.assertEqual(self.request(engine, 1, {"op": "down", "vk": 13, "t": 250})["commit"], "卯")
        self.assertEqual(self.type_a(engine, 2)["composition"], "卯")  # learned across sessions
        # Shift+F7 undoes only what this session confirmed.
        self.request(engine, 2, {"op": "down", "vk": 0x76, "t": 300, "mods": ["shift"]})
        self.request(engine, 2, {"op": "up", "vk": 0x76, "t": 310})
        self.assertEqual(len(path.read_text().splitlines()), 1)
        self.request(engine, 1, {"op": "down", "vk": 0x76, "t": 300, "mods": ["shift"]})
        self.request(engine, 1, {"op": "up", "vk": 0x76, "t": 310})
        self.request(engine, 2, {"op": "reset"})
        self.assertEqual(self.type_a(engine, 2, 400)["composition"], "宇")


class SlowScorer:
    """Prefers 卯 over the Viterbi 宇; each call sleeps (a slow model)."""

    def __init__(self, sleep=0.2):
        self.sleep = sleep

    def nll(self, context, candidates):
        time.sleep(self.sleep)
        return [0.0 if c.startswith("卯") else 10.0 for c in candidates]


class LmPipelineTest(EngineCase):
    """Design 2026-11-12 §7 tests 7-8, through the engine's sessions."""

    def wait_state(self, worker, state):
        deadline = time.monotonic() + 5
        while worker.state != state and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(worker.state, state)

    def test_pending_reply_then_poll_then_replay(self):
        loaded = loaded_session()
        worker = LmWorker(lambda: LmRescorer(SlowScorer()), loaded._lex)
        self.wait_state(worker, "ready")
        engine = self.engine(loaded, worker)
        self.open(engine, 1)
        self.type_a(engine, 1)
        self.request(engine, 1, {"op": "down", "vk": 32, "t": 100})
        start = time.monotonic()
        reply = self.request(engine, 1, {"op": "up", "vk": 32, "t": 130})
        self.assertLess(time.monotonic() - start, 0.1)  # the engine answers at once
        self.assertEqual((reply["pending"], reply["converted"], reply["composition"],
                          reply["candidates"], reply["timer"]), (True, False, "宇", [], 140))
        t = 130
        while reply["pending"]:
            time.sleep(0.016)
            t += 16
            reply = self.request(engine, 1, {"op": "timeout", "t": t})
        self.assertEqual((reply["converted"], reply["composition"]), (True, "卯"))
        self.assertEqual(self.request(engine, 1, {"op": "down", "vk": 13, "t": 900})["commit"], "卯")
        # A key typed while the next conversion is pending waits behind it.
        self.type_a(engine, 1, 1000)
        self.request(engine, 1, {"op": "down", "vk": 32, "t": 1100})
        self.assertTrue(self.request(engine, 1, {"op": "up", "vk": 32, "t": 1130})["pending"])
        reply = self.type_a(engine, 1, 1140)
        self.assertEqual((reply["pending"], reply["composition"], reply["commit"]), (True, "宇", ""))
        t = 1200
        while reply["pending"]:
            time.sleep(0.016)
            reply = self.request(engine, 1, {"op": "timeout", "t": t})
            t += 16
        self.assertEqual((reply["commit"], reply["composition"], reply["converted"]),
                         ("卯", "宇", False))

    def test_serves_while_the_lm_loads_slowly_or_fails(self):
        release = threading.Event()

        def slow():
            release.wait(5)
            return LmRescorer(SlowScorer(0))

        def broken():
            raise ImportError("no torch")

        for name, loader in (("slow", slow), ("broken", broken)):
            with self.subTest(loader=name), redirect_stderr(io.StringIO()):
                loaded = loaded_session()
                engine = self.engine(loaded)
                worker = LmWorker(loader, loaded._lex, on_disabled=engine.lm_disabled)
                engine.worker = worker
                if name == "broken":
                    self.wait_state(worker, "disabled")
                    self.assertEqual(engine.channel.warnings, ["lm"])  # §6.2: the user is told
                self.open(engine, 1)
                self.type_a(engine, 1)
                self.request(engine, 1, {"op": "down", "vk": 32, "t": 100})
                reply = self.request(engine, 1, {"op": "up", "vk": 32, "t": 130})
                self.assertEqual((reply["pending"], reply["converted"], reply["composition"]),
                                 (False, True, "宇"))  # the Viterbi 1-best meanwhile
                if name == "slow":
                    release.set()
                    self.wait_state(worker, "ready")
                    self.request(engine, 1, {"op": "down", "vk": 27, "t": 200})
                    self.request(engine, 1, {"op": "down", "vk": 32, "t": 300})
                    reply = self.request(engine, 1, {"op": "up", "vk": 32, "t": 330})
                    t = 330
                    while reply["pending"]:
                        time.sleep(0.016)
                        t += 16
                        reply = self.request(engine, 1, {"op": "timeout", "t": t})
                    self.assertEqual(reply["composition"], "卯")


class LoggingTest(EngineCase):
    def test_no_text_in_the_log_unless_asked(self):
        for log_text in (False, True):
            with self.subTest(log_text=log_text):
                settings = Namespace(**{**vars(SETTINGS), "log_text": log_text})
                engine = self.engine(settings=settings)
                self.open(engine, 1)
                log = io.StringIO()
                with redirect_stderr(log):
                    self.type_a(engine, 1)
                    self.request(engine, 1, {"op": "down", "vk": 13})
                if log_text:
                    self.assertIn("宇", log.getvalue())
                else:
                    self.assertEqual(log.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
