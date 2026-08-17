"""Parse Korean/numeric calendar phrases into bindable time prefixes.

Generic: no table or column names. Callers decide which date column to bind.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

_YEAR = re.compile(r"(?P<year>\d{2,4})\s*년")
_MONTH = re.compile(r"(?P<month>1[0-2]|0?[1-9])\s*월")
_DAY = re.compile(r"(?P<day>3[01]|[12]\d|0?[1-9])\s*일")
_WEEK = re.compile(
    r"(?P<last>마지막)\s*주|"
    r"(?P<ord>첫째|둘째|셋째|넷째|다섯째)\s*주|"
    r"제?\s*(?P<num>[1-5])\s*주(?:차)?(?!간)"
)
_ORDINAL = {
    "첫째": 1,
    "둘째": 2,
    "셋째": 3,
    "넷째": 4,
    "다섯째": 5,
}


@dataclass(frozen=True)
class ParsedPeriod:
    year: int
    month: int | None = None
    day: int | None = None
    week_start: date | None = None
    week_end: date | None = None

    @property
    def grain(self) -> str:
        if self.week_start is not None:
            return "week"
        if self.day is not None:
            return "day"
        if self.month is not None:
            return "month"
        return "year"

    @property
    def like_prefix(self) -> str:
        if self.day is not None and self.month is not None:
            return f"{self.year:04d}{self.month:02d}{self.day:02d}"
        if self.month is not None:
            return f"{self.year:04d}{self.month:02d}"
        return f"{self.year:04d}"

    def start_date(self) -> date:
        if self.week_start is not None:
            return self.week_start
        if self.day is not None and self.month is not None:
            return date(self.year, self.month, self.day)
        if self.month is not None:
            return date(self.year, self.month, 1)
        return date(self.year, 1, 1)

    def end_date_exclusive(self) -> date:
        if self.week_end is not None:
            return self.week_end + timedelta(days=1)
        start = self.start_date()
        if self.day is not None and self.month is not None:
            return start + timedelta(days=1)
        if self.month is not None:
            if start.month == 12:
                return date(start.year + 1, 1, 1)
            return date(start.year, start.month + 1, 1)
        return date(start.year + 1, 1, 1)


def _normalize_year(raw: int) -> int:
    if raw < 100:
        return 2000 + raw
    return raw


def extract_year(text: str | None) -> int | None:
    match = _YEAR.search(text or "")
    if not match:
        return None
    return _normalize_year(int(match.group("year")))


def week_mention(text: str | None) -> bool:
    return bool(_WEEK.search(str(text or "")))


def _week_ordinal(source: str) -> int | None:
    match = _WEEK.search(source)
    if not match:
        return None
    if match.group("last"):
        return -1
    named = match.group("ord")
    if named:
        return _ORDINAL[named]
    return int(match.group("num"))


def _iso_weeks_from_month_first(year: int, month: int) -> list[tuple[date, date]]:
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    monday = first - timedelta(days=first.isoweekday() - 1)
    weeks: list[tuple[date, date]] = []
    while monday <= last:
        sunday = monday + timedelta(days=6)
        weeks.append((monday, sunday))
        monday += timedelta(days=7)
    return weeks


def parse_korean_period(text: str | None, *, fallback_year: int | None = None) -> ParsedPeriod | None:
    """Parse '2025년 9월', '25년', '10월' (year from fallback), '2025년 9월 15일', '2025년 10월 셋째 주'."""

    source = str(text or "").strip()
    if not source:
        return None
    year_match = _YEAR.search(source)
    month_match = _MONTH.search(source)
    day_match = _DAY.search(source)
    ordinal = _week_ordinal(source)
    year = _normalize_year(int(year_match.group("year"))) if year_match else fallback_year
    month = int(month_match.group("month")) if month_match else None
    if ordinal is not None:
        if year is None or month is None or not 1 <= month <= 12:
            return None
        weeks = _iso_weeks_from_month_first(year, month)
        if ordinal == -1:
            start, end = weeks[-1]
        elif 1 <= ordinal <= len(weeks):
            start, end = weeks[ordinal - 1]
        else:
            return None
        return ParsedPeriod(
            year=year,
            month=month,
            week_start=start,
            week_end=end,
        )
    if not year_match and not month_match and not day_match:
        return None
    if year is None:
        return None
    day = int(day_match.group("day")) if day_match else None
    if day is not None and month is None:
        return None
    if month is not None and not 1 <= month <= 12:
        return None
    if day is not None:
        last = calendar.monthrange(year, month)[1]
        if not 1 <= day <= last:
            return None
    return ParsedPeriod(year=year, month=month, day=day)
