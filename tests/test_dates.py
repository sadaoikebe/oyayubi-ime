"""Date and time spellings (U3)."""
from __future__ import annotations

import unittest
from datetime import datetime

from yuragi.ime.bunsetsu import candidates
from yuragi.ime.dates import date_spellings
from yuragi.lattice.mozc_dict import BOS_ID, load_lex
from yuragi.lattice.table import load_table, reverse_table

NOW = datetime(2026, 11, 12, 9, 5)  # a Thursday


class DateSpellingTest(unittest.TestCase):
    def test_days(self):
        self.assertEqual(date_spellings("きょう", NOW),
                         ["2026/11/12", "2026-11-12", "2026年11月12日", "11月12日",
                          "11/12", "令和8年11月12日", "木曜日"])
        self.assertEqual(date_spellings("あした", NOW)[2], "2026年11月13日")
        self.assertEqual(date_spellings("おととい", NOW)[-1], "火曜日")
        # Month and year boundaries come from the calendar.
        self.assertEqual(date_spellings("あした", datetime(2026, 12, 31))[0], "2027/01/01")

    def test_years_and_now(self):
        self.assertEqual(date_spellings("ことし", NOW), ["2026年", "令和8年"])
        self.assertEqual(date_spellings("らいねん", datetime(2018, 5, 1)),
                         ["2019年", "令和元年"])
        self.assertEqual(date_spellings("きょねん", datetime(2018, 5, 1)), ["2017年"])
        self.assertEqual(date_spellings("いま", NOW), ["9:05", "9時5分"])

    def test_other_readings(self):
        self.assertEqual(date_spellings("きょうと", NOW), [])
        self.assertEqual(date_spellings("", NOW), [])


class DateCandidateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lex = load_lex()
        cls.reverse = reverse_table(load_table())

    def rows(self, reading, now=NOW):
        tokens = [self.reverse[c] for c in reading]
        return list(candidates(tokens, self.lex, BOS_ID, BOS_ID, now=now))

    def test_after_every_other_row(self):
        rows = self.rows("きょう")
        dates = [c.surface for c in rows if c.kind == "date"]
        self.assertEqual(dates, date_spellings("きょう", NOW))
        # Appended: every other row keeps its place (and its number key).
        without = self.rows("きょう", None)
        self.assertEqual(rows[:len(without)], without)
        self.assertTrue(all(c.kind == "date" for c in rows[len(without):]))

    def test_kana_tail_is_kept(self):
        surfaces = [c.surface for c in self.rows("きょうは") if c.kind == "date"]
        self.assertIn("2026/11/12は", surfaces)
        self.assertIn("木曜日は", surfaces)

    def test_no_date_inside_another_word(self):
        surfaces = {c.surface for c in self.rows("きょうと") if c.kind == "date"}
        self.assertNotIn("2026/11/12", surfaces)
        self.assertTrue(all(s.endswith("と") for s in surfaces))  # only 今日と's tail


if __name__ == "__main__":
    unittest.main()
