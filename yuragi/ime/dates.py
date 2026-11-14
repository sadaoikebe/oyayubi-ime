"""Date and time spellings for relative day/year words (a date rewriter, U3).

They are extra candidate rows after the dictionary spellings, so the 1-best
composition is unchanged. They depend on the clock and are never learned.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

WEEKDAYS = "月火水木金土日"
REIWA_START = 2019  # 令和元年

DAY_OFFSETS = {"おととい": -2, "おとつい": -2, "きのう": -1, "さくじつ": -1,
               "きょう": 0, "ほんじつ": 0, "あした": 1, "あす": 1, "みょうにち": 1,
               "あさって": 2, "しあさって": 3}
YEAR_OFFSETS = {"おととし": -2, "きょねん": -1, "さくねん": -1, "ことし": 0,
                "ほんねん": 0, "らいねん": 1, "さらいねん": 2}
NOW_READINGS = {"いま"}


def _era(year: int) -> str:
    n = year - REIWA_START + 1
    return f"令和{'元' if n == 1 else n}年"


def day_spellings(d: date) -> list[str]:
    return [f"{d.year}/{d.month:02d}/{d.day:02d}", f"{d.year}-{d.month:02d}-{d.day:02d}",
            f"{d.year}年{d.month}月{d.day}日", f"{d.month}月{d.day}日",
            f"{d.month}/{d.day}", f"{_era(d.year)}{d.month}月{d.day}日",
            f"{WEEKDAYS[d.weekday()]}曜日"]


def date_spellings(reading: str, now: datetime) -> list[str]:
    """Spellings for a relative date/time reading; [] for any other reading."""
    if reading in DAY_OFFSETS:
        return day_spellings(now.date() + timedelta(days=DAY_OFFSETS[reading]))
    if reading in YEAR_OFFSETS:
        year = now.year + YEAR_OFFSETS[reading]
        return [f"{year}年", _era(year)] if year >= REIWA_START else [f"{year}年"]
    if reading in NOW_READINGS:
        return [f"{now.hour}:{now.minute:02d}", f"{now.hour}時{now.minute}分"]
    return []
