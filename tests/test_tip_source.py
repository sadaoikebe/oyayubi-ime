"""REDESIGN §10 stage 3: what the audit (§3.1) and the redesign (§7.1) removed
from the TIP stays out of its source.

The TIP runs only inside TSF, so this checks the source text itself: no
process management besides starting the broker, no Python, no TCP, no
swallowed exceptions, no constant timer, no investigation leftovers.
"""
from pathlib import Path
import re
import unittest

TIP = Path(__file__).resolve().parents[1] / "src" / "tip" / "yuragi_tip.cpp"

# (what was removed, a pattern that would find it)
REMOVED = [
    ("the respawn loop Server::Run and its Server struct", r"\bstruct Server\b|Server::Run"),
    ("SpawnResident / ResolvePython / FindPython / SpawnAdminUi",
     r"SpawnResident|ResolvePython|FindPython|SpawnAdminUi"),
    ("any Python started by the TIP", r"yuragi\.ime\.|pythonw?\.exe|py\.exe"),
    ("the developer's own path", r"marur"),
    ("PATH search for a program", r"SearchPathW"),
    ("TCP and Winsock", r"winsock|ws2_32|WSAStartup|\bsocket\(|closesocket|\brecv\(|MSG_PEEK"),
    ("endpoint.json and its token", r"endpoint\.json|\"token\""),
    ("the strict reply check", r"ParseReply|object\.size\(\)\s*!=\s*3"),
    ("structured exception swallowing", r"__try|__except"),
    ("the 16 ms constant timer", r"SetTimer\([^;]*,\s*16\s*,"),
    ("the Alt+` investigation log", r"TraceAltToggle"),
    ("dropping the connection when the app refuses an edit", r"edit rejected|Die\("),
    ("dropping the connection and the text on a slow reply", r"fail-open|ClearLocal"),
    ("COM registration under HKCU", r"RegisterComRoot\(HKEY_CURRENT_USER|HKEY_CURRENT_USER,\s*clsid"),
    ("the SECUREMODE clean-up", r"SECUREMODE"),
    ("the clock fallback", r"QuadPart\s*==\s*0"),
    ("the HOMEDRIVE fallback", r"HOMEDRIVE|HOMEPATH"),
    ("claiming UI element support", r"UIELEMENTENABLED"),
    ("a password check on every key", r"PasswordScope\("),
]


class RemovedFromTipTest(unittest.TestCase):
    def setUp(self):
        text = TIP.read_text(encoding="utf-8")
        # Comments may name what is gone; only code counts.
        text = re.sub(r"//[^\n]*", "", text)
        self.code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)

    def test_removed_items_stay_removed(self):
        for what, pattern in REMOVED:
            with self.subTest(what=what):
                self.assertIsNone(re.search(pattern, self.code), what)

    def test_the_only_process_it_starts_is_the_broker(self):
        starts = re.findall(r"CreateProcess\w*\(", self.code)
        self.assertEqual(len(starts), 1)
        self.assertIn('L"\\\\yuragi-broker.exe"', self.code)
        # §7.3: no handles inherited, away from the application's job, no window.
        call = self.code[self.code.index("CreateProcessW("):]
        call = call[:call.index(";")]
        self.assertIn("FALSE", call)  # bInheritHandles
        self.assertIn("CREATE_BREAKAWAY_FROM_JOB", call)
        self.assertIn("CREATE_NO_WINDOW", call)

    def test_timers_ring_once(self):
        # Every SetTimer is one-shot: the window procedure kills each timer when it rings.
        self.assertIn("KillTimer(h, w);", self.code)


if __name__ == "__main__":
    unittest.main()
