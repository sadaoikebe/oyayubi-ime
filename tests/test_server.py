from __future__ import annotations

import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from oyayubi.ime import server
from oyayubi.ime.session import ImeSession
from tools.wp0.mozc_dict import MozcLex
from tools.wp0.table import load_table


class Stream(io.StringIO):
    def reconfigure(self, **kwargs):
        pass


class ServerTest(unittest.TestCase):
    def run_server(self, log_text):
        events = []
        session = SimpleNamespace(
            load=lambda: events.append("load"),
            on_key=lambda *args: "確定秘密",
            composition="編集中の秘密", converted=False, tokens=[], candidates=[], cand_index=0,
            direct=False,
            lm=None,
            fsm=SimpleNamespace(timer_deadline=None, state="S0"),
            segment_snapshot=lambda: [], focus=-1, candidate_page=0,
            candidate_kinds=[], has_next_page=False,
        )
        stdout, stderr = Stream(), Stream()
        stdin = Stream('{"op":"down","vk":74}\n{"op":"quit"}\n')
        with patch.object(server, "ImeSession", return_value=session), \
             patch.object(server.gc, "freeze", side_effect=lambda: events.append("freeze")), \
             patch.object(server.sys, "stdin", stdin), \
             patch.object(server.sys, "stdout", stdout), \
             patch.object(server.sys, "stderr", stderr), \
             patch.object(server.sys, "argv", ["server"] + (["--log-text"] if log_text else [])):
            server.main()
        self.assertEqual(events, ["load", "freeze"])
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertTrue(replies[0]["ok"])
        self.assertEqual(replies[1]["composition"], "編集中の秘密")
        self.assertEqual(replies[1]["commit"], "確定秘密")
        return stderr.getvalue()

    def test_no_body_log_by_default(self):
        self.assertEqual(self.run_server(False), "")

    def test_explicit_body_log(self):
        log = self.run_server(True)
        self.assertIn("編集中の秘密", log)
        self.assertIn("確定秘密", log)

    def test_reset_and_modifier_snapshot_on_stdio(self):
        session = ImeSession(_table=load_table(), _lex=MozcLex({}, set(), [0] * 4, 2),
                             learning_path=None)
        requests = [
            {"op": "down", "vk": 65, "t": 0, "mods": ["shift"]},
            {"op": "reset", "t": 10},
            {"op": "up", "vk": 65, "t": 20},
            {"op": "timeout", "t": 100},
            {"op": "down", "vk": 65, "t": 200, "mods": []},
            {"op": "up", "vk": 65, "t": 230, "mods": []},
            {"op": "down", "vk": 13, "t": 300},
            {"op": "quit"},
        ]
        stdout, stderr = Stream(), Stream()
        stdin = Stream("".join(json.dumps(request) + "\n" for request in requests))
        with patch.object(server, "ImeSession", return_value=session), \
             patch.object(server.gc, "freeze"), \
             patch.object(server.sys, "stdin", stdin), \
             patch.object(server.sys, "stdout", stdout), \
             patch.object(server.sys, "stderr", stderr), \
             patch.object(server.sys, "argv", ["server"]):
            server.main()
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()][1:]
        self.assertEqual(replies[0]["composition"], "A")
        for reply in replies[1:4]:
            self.assertEqual(reply, {
                "composition": "", "commit": "", "converted": False, "fsm": "S1",
                "n_tokens": 0, "timer": None, "candidates": [], "cand_index": 0,
                "segments": [], "focus": -1, "candidate_page": 0,
                "candidate_kinds": [], "has_next_page": False,
                "direct": False, "pass_through": False,
            })
        self.assertEqual(replies[-1]["commit"], "う")
        self.assertEqual(stderr.getvalue(), "")

    def test_direct_dispatch_and_reset(self):
        session = ImeSession(_table=load_table(), _lex=MozcLex({}, set(), [0] * 4, 2),
                             learning_path=None)
        def send(op, **kwargs):
            return server.dispatch(session, {"op": op, **kwargs})

        send("down", vk=65, t=0, mods=["shift"])
        self.assertEqual(session.composition, "A")
        reply = send("set_direct", direct=False)
        self.assertEqual(reply["composition"], "A")
        reply = send("set_direct", direct=True)
        self.assertEqual((reply["direct"], reply["composition"], reply["commit"]),
                         (True, "", ""))
        for op in ("down", "up"):
            reply = send(op, vk=65, t=20)
            self.assertTrue(reply["pass_through"])
            self.assertEqual((reply["composition"], reply["commit"], reply["timer"]),
                             ("", "", None))
        self.assertTrue(send("reset")["direct"])
        self.assertEqual(send("timeout", t=100)["composition"], "")
        self.assertFalse(send("toggle_direct")["direct"])
        self.assertFalse(send("down", vk=65, t=200)["pass_through"])
        send("up", vk=65, t=230)
        self.assertEqual(send("down", vk=13, t=300)["commit"], "う")
        with self.assertRaises(ValueError):
            send("set_direct", direct="false")

    def test_direct_transition_discards_pending_chord(self):
        session = ImeSession(_table=load_table(), _lex=MozcLex({}, set(), [0] * 4, 2),
                             learning_path=None)
        send = lambda msg: server.dispatch(session, msg)
        self.assertIsNotNone(send({"op": "down", "vk": 65, "t": 0})["timer"])
        reply = send({"op": "toggle_direct"})
        self.assertEqual((reply["direct"], reply["timer"], reply["commit"]), (True, None, ""))
        send({"op": "toggle_direct"})
        self.assertEqual(send({"op": "up", "vk": 65, "t": 10})["composition"], "")
        self.assertEqual(send({"op": "timeout", "t": 200})["composition"], "")

    def test_context_dispatch_on_stdio_path(self):
        session = ImeSession(learning_path=None)
        server.dispatch(session, {"op": "context", "text": "前" * 300})
        self.assertEqual(session._lm_context, "前" * 200)
        server.dispatch(session, {"op": "reset"})
        self.assertEqual(session._lm_context, "")


if __name__ == "__main__":
    unittest.main()
