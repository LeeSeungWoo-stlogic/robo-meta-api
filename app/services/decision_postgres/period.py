"""Parse Korean/numeric calendar phrases into bindable time prefixes.

Generic: no table or column names. Callers decide which date column to bind.
Relative phrases use the host clock in Asia/Seoul.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_YEAR = re.compile(r"(?P<year>\d{2,4})\s*년")
_MONTH = re.compile(r"(?P<month>1[0-2]|0?[1-9])\s*월")
_DAY = re.compile(r"(?P<day>3[01]|[12]\d|0?[1-9])\s*일")
_WEEK = re.compile(
    r"(?P<last>마지막)\s*주|"
    r"(?P<ord>첫째|둘째|셋째|넷째|다섯째)\s*주|"
    r"제?\s*(?P<num>[1-5])\s*주(?:차)?(?!간)"
)
_RECENT_SPAN = re.compile(
    r"(?:최근|지난)\s*(?P<n>\d+)\s*(?P<unit>년|개월|달|주|일)"
)
_ORDINAL = {
    "첫째": 1,
    "둘째": 2,
    "셋째": 3,
    "넷째": 4,
    "다섯째": 5,
}
_DECISION_TZ = ZoneInfo("Asia/Seoul")
_DURATION_ANCHOR = re.compile(r"최근|지난")
_RELATIVE_MARKER = re.compile(
    r"최근|지난|어제|그저께|그제|오늘|금일|내일|모레|"
    r"올해|금년|작년|지난해|재작년|내년|후년|"
    r"이번달|이달|지난달|익월|다음달|내달|"
    r"이번주|지난주|다음주|내주|"
    r"하루|이틀|사흘|나흘|닷새|엿새|이레|여드레|아흐레|열흘|보름|"
    r"일주일|한달|한주"
)
# Longest first. (compact form, unit, count)
_DURATION_WORDS: tuple[tuple[str, str, int], ...] = (
    ("일주일간", "일", 7),
    ("일주일", "일", 7),
    ("한달간", "개월", 1),
    ("한달", "개월", 1),
    ("한주간", "일", 7),
    ("한주", "일", 7),
    ("두달간", "개월", 2),
    ("석달", "개월", 3),
    ("세달", "개월", 3),
    ("두달", "개월", 2),
    ("삼주", "일", 21),
    ("세주", "일", 21),
    ("이주", "일", 14),
    ("두주", "일", 14),
    ("한해", "년", 1),
    ("일년", "년", 1),
    ("여드레", "일", 8),
    ("아흐레", "일", 9),
    ("열흘", "일", 10),
    ("보름", "일", 15),
    ("닷새", "일", 5),
    ("엿새", "일", 6),
    ("이레", "일", 7),
    ("사흘", "일", 3),
    ("나흘", "일", 4),
    ("이틀", "일", 2),
    ("하루", "일", 1),
)


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


def decision_today() -> date:
    """Host clock in Korea Standard Time. Not the client clock."""

    return datetime.now(_DECISION_TZ).date()


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


def _iso_week(today: date, *, offset_weeks: int = 0) -> ParsedPeriod:
    monday = today - timedelta(days=today.isoweekday() - 1)
    monday = monday + timedelta(days=7 * offset_weeks)
    sunday = monday + timedelta(days=6)
    return ParsedPeriod(
        year=monday.year,
        month=monday.month,
        week_start=monday,
        week_end=sunday,
    )


def _shift_months(origin: date, months: int) -> date:
    month_index = origin.year * 12 + (origin.month - 1) + months
    year, month0 = divmod(month_index, 12)
    last = calendar.monthrange(year, month0 + 1)[1]
    return date(year, month0 + 1, min(origin.day, last))


def _span_to(start: date, end: date) -> ParsedPeriod:
    if start > end:
        start, end = end, start
    return ParsedPeriod(
        year=start.year,
        month=start.month,
        week_start=start,
        week_end=end,
    )


def _duration_start(today: date, unit: str, count: int) -> date | None:
    if count <= 0:
        return None
    if unit == "년":
        return date(today.year - count, today.month, today.day)
    if unit in {"개월", "달"}:
        return _shift_months(today, -count)
    if unit == "주":
        return today - timedelta(days=7 * count)
    return today - timedelta(days=count)


def parse_korean_period(
    text: str | None,
    *,
    fallback_year: int | None = None,
    today: date | None = None,
) -> ParsedPeriod | None:
    """Parse '2025년 9월', '최근 일주일', '익월', '나흘'(with 최근/지난).

    Duration words without 최근/지난 do not bind. Bare '최근' does not.
    """

    source = str(text or "").strip()
    if not source:
        return None
    clock = today or decision_today()
    year_match = _YEAR.search(source)
    month_match = _MONTH.search(source)
    day_match = _DAY.search(source)
    if _RELATIVE_MARKER.search(re.sub(r"\s+", "", source)) and not year_match:
        relative = _parse_relative_period(source, today=clock)
        if relative is not None:
            return relative
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
        return _parse_relative_period(source, today=clock)
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


def parse_period_from_query(
    query: str,
    period_text: str | None = None,
    *,
    today: date | None = None,
) -> ParsedPeriod | None:
    """Prefer the period slot, then the full question. Same clock."""

    slot = str(period_text or "").strip()
    if slot:
        parsed = parse_korean_period(slot, today=today)
        if parsed is not None:
            return parsed
    return parse_korean_period(query, today=today)


def _parse_relative_period(source: str, *, today: date) -> ParsedPeriod | None:
    recent = _RECENT_SPAN.search(source)
    if recent:
        count = int(recent.group("n"))
        start = _duration_start(today, recent.group("unit"), count)
        if start is None:
            return None
        return _span_to(start, today)

    compact = re.sub(r"\s+", "", source)
    if _DURATION_ANCHOR.search(compact):
        for word, unit, count in _DURATION_WORDS:
            if word in compact:
                start = _duration_start(today, unit, count)
                if start is None:
                    return None
                return _span_to(start, today)

    if "그저께" in compact or "그제" in compact:
        target = today - timedelta(days=2)
        return ParsedPeriod(year=target.year, month=target.month, day=target.day)
    if "모레" in compact:
        target = today + timedelta(days=2)
        return ParsedPeriod(year=target.year, month=target.month, day=target.day)
    if "내일" in compact:
        target = today + timedelta(days=1)
        return ParsedPeriod(year=target.year, month=target.month, day=target.day)
    if "어제" in compact:
        target = today - timedelta(days=1)
        return ParsedPeriod(year=target.year, month=target.month, day=target.day)
    if "오늘" in compact or "금일" in compact:
        return ParsedPeriod(year=today.year, month=today.month, day=today.day)
    if "재작년" in compact:
        return ParsedPeriod(year=today.year - 2)
    if "후년" in compact:
        return ParsedPeriod(year=today.year + 2)
    if "내년" in compact:
        return ParsedPeriod(year=today.year + 1)
    if "작년" in compact or "지난해" in compact:
        return ParsedPeriod(year=today.year - 1)
    if "올해" in compact or "금년" in compact:
        return ParsedPeriod(year=today.year)
    if "지난달" in compact:
        start = _shift_months(date(today.year, today.month, 1), -1)
        return ParsedPeriod(year=start.year, month=start.month)
    if "익월" in compact or "다음달" in compact or "내달" in compact:
        start = _shift_months(date(today.year, today.month, 1), 1)
        return ParsedPeriod(year=start.year, month=start.month)
    if "이번달" in compact or compact == "이달":
        return ParsedPeriod(year=today.year, month=today.month)
    if "지난주" in compact:
        return _iso_week(today, offset_weeks=-1)
    if "다음주" in compact or "내주" in compact:
        return _iso_week(today, offset_weeks=1)
    if "이번주" in compact:
        return _iso_week(today, offset_weeks=0)
    return None
