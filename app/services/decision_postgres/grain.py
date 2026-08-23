"""Query-time measurement grain (month/day/hour/instant).

Maps period language to a fact *role*, never to a physical table name.
Fallback is the next finer grain family (month → day → hour), still by
tokens like 01mm/01dd/01hh and Korean grain words — not rdd01dd_tb.
"""

from __future__ import annotations

import re
from typing import Literal

from ...schemas import QueryAnalysis, SchemaRoleRequirement

TimeGrain = Literal["month", "day", "hour", "instant"]

PERIOD_FACT_SEARCH_MIN_LIMIT = 12

GRAIN_SPECS: dict[TimeGrain, tuple[str, list[str]]] = {
    "month": (
        "월별 계측 팩트",
        ["월별", "월 단위", "01mm", "한달", "월 집계"],
    ),
    "day": (
        "일별 계측 팩트",
        ["일별", "일 단위", "일자", "01dd", "하루"],
    ),
    "hour": (
        "시간별 계측 팩트",
        ["시간별", "시간 단위", "01hh", "매시간"],
    ),
    "instant": (
        "실시간 계측 팩트",
        ["실시간", "순시", "시점", "로그 시각"],
    ),
}

GENERIC_FACT_ROLE = "계측 팩트"
GENERIC_FACT_TERMS = ["계측값", "집계", "시계열", "01mm", "01dd", "01hh"]

_FALLBACK: dict[TimeGrain, tuple[TimeGrain, ...]] = {
    "month": ("day", "hour"),
    "day": ("hour",),
    "hour": ("day",),
    "instant": (),
}

_EXPLICIT: tuple[tuple[TimeGrain, tuple[str, ...]], ...] = (
    ("instant", ("실시간", "순시", "현재값")),
    ("hour", ("시간별", "시간 단위", "매시간")),
    ("day", ("일별", "일자별", "하루 단위")),
    ("month", ("월별", "월 단위", "월 집계")),
)

_MONTH_PERIOD = re.compile(
    r"(?:\d{2,4}\s*년\s*)?\d{1,2}\s*월|\d+\s*개월|지난\s*달|한\s*달"
)
_DAY_PERIOD = re.compile(
    r"어제|그제|오늘|금일|\d{1,2}\s*일(?:간|동안|치)?"
)
_HOUR_PERIOD = re.compile(r"\d+\s*시간|한\s*시간")
_DAY_ANSWER = re.compile(
    r"날짜\s*알려|날짜를\s*알려|날짜가\s*(?:언제|있어)|"
    r"가장\s*(?:높은|낮은|많았던|적었던)\s*날짜|"
    r"높았던\s*날짜|낮았던\s*날짜|"
    r"인\s*날짜|인날|아닌날|"
    r"날이\s*언제|날\s*알려"
)
_CLOCK_AGG = ("시간별", "매시간", "시간 단위")
_WINDOW_EVENT = ("떨어진", "떨어졌", "넘어간", "넘었", "초과한", "초과했")
_TREND = ("추이", "추세", "트렌드", "변화")

_PERIOD_AREA_EXCLUDE = frozenset({"raw", "hist", "link"})


def fallback_grains(grain: str | None) -> tuple[TimeGrain, ...]:
    key = str(grain or "").strip().lower()
    if key not in _FALLBACK:
        return ()
    return _FALLBACK[key]  # type: ignore[index, return-value]


def explicit_time_grain(query: str) -> TimeGrain | None:
    """Grain words the question locked. Period-only '8월' is not explicit."""

    q = query or ""
    for grain, terms in _EXPLICIT:
        if any(term in q for term in terms):
            return grain
    return None


_GRAIN_KO: dict[TimeGrain, str] = {
    "month": "월",
    "day": "일",
    "hour": "시간",
    "instant": "실시간",
}


def empty_result_fallback_grain(query: str) -> TimeGrain | None:
    """After a 0-row execute, retry the next finer family once.

    Explicit 월별/일별/시간별 stays on that grain. Period-inferred grain
    may fall back once (month→day, day→hour, hour→day).
    """

    if explicit_time_grain(query) is not None:
        return None
    grain = resolve_time_grain(query)
    finer = fallback_grains(grain)
    return finer[0] if finer else None


def grain_fallback_reason(source: TimeGrain | None, dest: TimeGrain | None) -> str:
    src = _GRAIN_KO[source] if source in _GRAIN_KO else (str(source or "").strip() or "질문")
    dst = _GRAIN_KO[dest] if dest in _GRAIN_KO else (str(dest or "").strip() or "더 고운")
    return f"{src} 팩트 0건으로 {dst} 팩트를 재조회했습니다"


def _asks_clock_answer(query: str) -> bool:
    if any(token in query for token in _CLOCK_AGG):
        return False
    return any(token in query for token in ("시간", "시각"))


def _asks_window_event(query: str) -> bool:
    return any(token in query for token in _WINDOW_EVENT)


def _asks_series(query: str) -> bool:
    return any(token in query for token in _TREND)


def year_window_narrows_to_month(query_grain: TimeGrain | None) -> bool:
    """A calendar year is a window. Do not override day/hour/instant answers."""

    return query_grain in (None, "month")


def resolve_time_grain(
    query: str,
    analysis: QueryAnalysis | None = None,
) -> TimeGrain | None:
    """Prefer explicit grain words, then period length. No table names."""

    del analysis
    q = query or ""
    for grain, terms in _EXPLICIT:
        if any(term in q for term in terms):
            return grain
    if _HOUR_PERIOD.search(q):
        return "hour"
    if _asks_clock_answer(q) and (
        any(token in q for token in ("일자", "날짜"))
        or _MONTH_PERIOD.search(q)
        or _DAY_PERIOD.search(q)
    ):
        return "hour"
    if _DAY_ANSWER.search(q):
        return "day"
    if any(token in q for token in ("시점", "그때", "그 당시")) and not any(
        token in q for token in _CLOCK_AGG
    ):
        if _MONTH_PERIOD.search(q):
            return "day"
    if _asks_window_event(q) and _MONTH_PERIOD.search(q):
        return "day"
    if _asks_series(q):
        if _DAY_PERIOD.search(q):
            return "hour"
        if _MONTH_PERIOD.search(q):
            return "day"
    if _MONTH_PERIOD.search(q):
        return "month"
    if _DAY_PERIOD.search(q):
        return "day"
    return None


def fact_role_for_grain(grain: TimeGrain | None) -> SchemaRoleRequirement:
    if grain is None:
        name, terms = GENERIC_FACT_ROLE, GENERIC_FACT_TERMS
    else:
        name, terms = GRAIN_SPECS[grain]
    return SchemaRoleRequirement(
        role=name,
        necessity="required",
        cardinality="many",
        search_terms=list(terms),
    )


def is_measurement_role(role: SchemaRoleRequirement) -> bool:
    """Fact/raw measurement roles, including HyDE names without 팩트."""

    if is_period_fact_role(role):
        return True
    text = f"{role.role} {' '.join(role.search_terms)}".casefold()
    if "마스터" in text:
        return False
    return any(token in text for token in ("계측", "측정", "팩트", "시계열", "집계"))


def is_period_fact_role(role: SchemaRoleRequirement) -> bool:
    text = f"{role.role} {' '.join(role.search_terms)}".casefold()
    if "마스터" in text:
        return False
    if "팩트" in text or "시계열" in text:
        return True
    return any(
        token in text
        for token in (
            "01dd",
            "01mm",
            "01hh",
            "일별",
            "월별",
            "시간별",
            "순시",
            "실시간",
        )
    )


def grain_from_fact_role(role: SchemaRoleRequirement) -> TimeGrain | None:
    text = f"{role.role} {' '.join(role.search_terms)}".casefold()
    if any(term in text for term in ("실시간", "순시", "시점")):
        return "instant"
    if any(term in text for term in ("월별", "월 단위", "01mm", "한달")):
        return "month"
    if any(term in text for term in ("시간별", "시간 단위", "01hh", "매시간")):
        return "hour"
    if any(term in text for term in ("일별", "일 단위", "일자", "01dd", "하루")):
        return "day"
    return None


def fallback_fact_roles(role: SchemaRoleRequirement) -> list[SchemaRoleRequirement]:
    grain = grain_from_fact_role(role)
    if grain is None:
        return [fact_role_for_grain(item) for item in ("month", "day", "hour")]
    return [fact_role_for_grain(item) for item in _FALLBACK.get(grain, ())]


def period_fact_search_limit(role: SchemaRoleRequirement, role_top_k: int) -> int:
    if not is_period_fact_role(role):
        return max(1, role_top_k)
    return max(int(role_top_k), PERIOD_FACT_SEARCH_MIN_LIMIT)


def period_fact_candidate_allowed(
    role: SchemaRoleRequirement,
    table: dict,
    *,
    subject_area: str,
) -> bool:
    """Period aggregates are not raw/hist/link shards. Instant grain may be raw."""

    if not is_period_fact_role(role):
        return True
    grain = grain_from_fact_role(role)
    if grain is None or grain == "instant":
        return True
    return subject_area not in _PERIOD_AREA_EXCLUDE
