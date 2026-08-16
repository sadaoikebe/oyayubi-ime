from __future__ import annotations

import unittest

from oyayubi.chord.fsm import AmbShift, Plain
from oyayubi.ime.session import ImeSession


class SessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = ImeSession()
        cls.s.load()

    def test_ohayou_tokens(self):
        self.s.tokens = [AmbShift("j"), Plain("h"), AmbShift("y"), Plain("a")]
        self.s._refresh()
        self.assertIn("おはよ", self.s.composition)

    def test_same_key_after_release_is_new_token(self):
        self.s.tokens.clear()
        self.s.fsm.reset()
        self.s.on_key(True, 0x57, 0)
        self.s.on_key(False, 0x57, 20)
        self.assertEqual(self.s.tokens, [Plain("w")])
        self.s.on_key(True, 0x20, 100)
        self.s.on_key(True, 0x57, 130)
        self.s.on_key(False, 0x20, 180)
        self.s.on_key(False, 0x57, 200)
        self.assertEqual(self.s.tokens, [Plain("w"), AmbShift("w")])
        self.assertIn("か", self.s.composition)

    def test_kaeru_keeps_ka(self):
        self.s.tokens.clear()
        self.s.fsm.reset()
        self.s.composition = ""
        # か = W 単独、え = Space+W、る = Space+I
        self.s.on_key(True, 0x57, 0)
        self.s.on_key(False, 0x57, 20)
        self.s.on_key(True, 0x20, 80)
        self.s.on_key(True, 0x57, 110)
        self.s.on_key(False, 0x20, 160)
        self.s.on_key(False, 0x57, 180)
        self.s.on_key(True, 0x20, 220)
        self.s.on_key(True, 0x49, 250)
        self.s.on_key(False, 0x20, 300)
        self.s.on_key(False, 0x49, 320)
        self.assertEqual(
            self.s.tokens, [Plain("w"), AmbShift("w"), AmbShift("i")]
        )
        self.assertTrue(
            "かえ" in self.s.composition or self.s.composition.startswith("変"),
            self.s.composition,
        )
