"""Press numbers and turns reach the lattice tokens (album design 2 §3.3)."""

from __future__ import annotations

import re
import unittest

from yuragi.chord.keys import VK_SPACE, VK_TO_M
from yuragi.ime.session import ImeSession
from yuragi.lattice.table import load_table

VK = {key: vk for vk, key in VK_TO_M.items()} | {"space": VK_SPACE}


def type_events(session: ImeSession, events: str) -> None:
    """"u+0 space+21 u-102": key, down/up, ms; due deadlines fire first."""
    for ev in events.split():
        name, op, t = re.fullmatch(r"(.+?)([+-])(\d+)", ev).groups()
        session.flush_timeout(int(t))
        session.on_key(op == "+", VK[name], int(t))
    session.flush_timeout(10**6)


class PressTokens(unittest.TestCase):
    def session(self) -> ImeSession:
        return ImeSession(_table=load_table(), learning_path=None)  # no dictionary

    def test_fused_chords_carry_press_and_turn(self):
        s = self.session()
        type_events(s, "u+0 space+21 u-102 f+140 space-205 f-225 a+225 a-300 "
                       "j+400 space+420 j-480 space-500")
        self.assertEqual(
            [(t["kind"], t["key"], t.get("press"), t.get("turn")) for t in s._lattice()],
            [("amb", "u", 1, 1), ("amb", "f", 1, 2), ("plain", "a", None, None),
             ("amb", "j", 2, 1)])

    def test_a_faceless_chord_takes_its_turn(self):
        # [ has no thumb faces: it is not in the lattice but it was a thumb stroke.
        s = self.session()
        type_events(s, "space+0 [+10 [-60 u+70 u-120 space-130")
        self.assertEqual([(t["key"], t["press"], t["turn"]) for t in s._lattice()],
                         [("u", 1, 2)])


if __name__ == "__main__":
    unittest.main()
