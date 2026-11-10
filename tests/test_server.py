from __future__ import annotations

import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from oyayubi.ime import server


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


if __name__ == "__main__":
    unittest.main()
