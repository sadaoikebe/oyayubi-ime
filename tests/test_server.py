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
        session = ImeSession(_table=load_table(), _lex=MozcLex({}, set(), [0] * 4, 2))
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
            })
        self.assertEqual(replies[-1]["commit"], "う")
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
