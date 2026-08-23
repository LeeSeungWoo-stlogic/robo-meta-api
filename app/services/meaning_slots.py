"""Meaning-slot helpers. Needles stay per-slot; never join into one haystack."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from ..schemas import QueryAnalysis
from .metadata_repository._search import SearchMixin

ALLOWED_PROCEDURES = frozenset({"lookup", "list", "aggregate", "extremum"})
_PHYSICAL_IDENT = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){2,}$"
)
_MAX_MENTION_COMPACT = 24
_AXIS_SUFFIXES = ("목록", "현황", "리스트")
_SENTENCE_MARKERS = (
    "하는",
    "하여",
    "해당하는",
    "또는",
    "그리고",
)
_PERIOD_PREFIX = re.compile(
    r"^(?:\d{2,4}년)?(?:\d{1,2}월)?(?:\d{1,2}일)?"
)
_TRAILING_EXCEPT = re.compile(r"(?:이외|외)$")
_OR_SPLIT = re.compile(r"\s*또는\s*")
_AGG_PHRASES = (
    "최댓값",
    "최솟값",
    "최대값",
    "최소값",
    "평균",
    "합계",
    "총합",
    "건수",
)
_MIN_EXTREMUM = (
    "제일 낮",
    "가장 낮",
    "제일 적",
    "가장 적",
    "최솟값",
    "최소값",
    "최소",
)
_MAX_EXTREMUM = (
    "제일 높",
    "가장 높",
    "제일 많",
    "가장 많",
    "최댓값",
    "최대값",
    "최고",
    "최대",
)
_EXTREMUM_LEADS = ("제일", "가장")
_EXTREMUM_MIN_TAILS = ("낮", "적")
_EXTREMUM_MAX_TAILS = ("높", "많")
_EXTREMUM_WINDOW = 16
_EXCLUDE_ANI = re.compile(
    r"(?P<body>[가-힣A-Za-z0-9]{2,24}?)(?:이|가|은|는)\s*아닌"
)
_EXCLUDE_JEWOE = re.compile(
    r"(?P<body>[가-힣A-Za-z0-9]{2,24})\s*제외"
)


@dataclass(frozen=True)
class RangeSlot:
    mention: str
    polarity: Literal["include", "exclude"]


def compact(text: str) -> str:
    return SearchMixin._compact_natural_text(text)


def extremum_function_from_text(text: str) -> str | None:
    """질문의 낮/적 → MIN, 높/많 → MAX. 가장/제일과 꼬리 사이에 짧은 간격 허용."""

    blob = str(text or "")
    if any(token in blob for token in _MIN_EXTREMUM):
        return "MIN"
    if any(token in blob for token in _MAX_EXTREMUM):
        return "MAX"
    key = compact(blob)
    for lead in _EXTREMUM_LEADS:
        start = 0
        while True:
            index = key.find(lead, start)
            if index < 0:
                break
            rest = key[index + len(lead) : index + len(lead) + _EXTREMUM_WINDOW]
            if any(tail in rest for tail in _EXTREMUM_MIN_TAILS):
                return "MIN"
            if any(tail in rest for tail in _EXTREMUM_MAX_TAILS):
                return "MAX"
            start = index + len(lead)
    return None


def measure_item_surface(text: str) -> str:
    """Drop 평균/합계 등 집계 말만 남기고 측정 항목 표면을 남긴다."""

    raw = str(text or "").strip()
    if not raw:
        return ""
    out = raw
    for phrase in _AGG_PHRASES:
        out = out.replace(phrase, " ")
    out = " ".join(out.split())
    return out or raw


def is_mention_needle(text: str) -> bool:
    """Needles are short surfaces. Definitions and clauses are not needles."""

    raw = str(text or "").strip()
    key = compact(raw)
    if len(key) < 2 or len(key) > _MAX_MENTION_COMPACT:
        return False
    if looks_physical_name(raw):
        return False
    if sum(1 for ch in raw if ch.isspace()) >= 2:
        return False
    if any(marker in key for marker in _SENTENCE_MARKERS):
        return False
    return True


def axis_mention(text: str) -> str:
    raw = str(text or "").strip()
    key = compact(raw)
    for suffix in _AXIS_SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix) + 1:
            stem = key[: -len(suffix)]
            if len(stem) >= 2:
                return stem
    return raw


def range_target_needle(target: str, primary_outputs: list[str]) -> str:
    raw = str(target or "").strip()
    key = compact(raw)
    if not key:
        return ""
    for item in primary_outputs:
        axis = compact(axis_mention(item))
        if not axis or key == axis:
            if key == axis:
                return ""
            continue
        if key.endswith(axis) and len(key) > len(axis) + 1:
            remainder = key[: -len(axis)]
            if is_mention_needle(remainder):
                return remainder
    if is_mention_needle(raw):
        return raw
    return ""


def unique_needles(values: list[str], *, limit: int = 20) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = compact(text)
        if not is_mention_needle(text) or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def meaning_failed(analysis: QueryAnalysis | None) -> bool:
    if analysis is None:
        return True
    return str(analysis.meaning_status or "").strip() == "failed"


def looks_physical_name(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    if raw.count(".") >= 2 and _PHYSICAL_IDENT.match(raw):
        return True
    return False


def is_answer_axis_text(text: str, primary_outputs: list[str]) -> bool:
    token = compact(text)
    if not token:
        return False
    for item in primary_outputs:
        other = compact(item)
        if not other:
            continue
        if token == other or other.startswith(token) or token.startswith(other):
            return True
    return False


def _exclude_bodies(text: str) -> list[str]:
    bodies: list[str] = []
    seen: set[str] = set()
    for pattern in (_EXCLUDE_ANI, _EXCLUDE_JEWOE):
        for match in pattern.finditer(text or ""):
            body = str(match.group("body") or "").strip()
            key = compact(body)
            if not key or key in seen:
                continue
            seen.add(key)
            bodies.append(body)
    return bodies


def _strip_exclude_clause(text: str) -> str:
    stripped = _EXCLUDE_ANI.sub(" ", text or "")
    stripped = _EXCLUDE_JEWOE.sub(" ", stripped)
    return " ".join(stripped.split())


def _or_parts(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    return [part.strip() for part in _OR_SPLIT.split(raw) if part.strip()]


def _strip_period_prefix(text: str) -> str:
    key = compact(text)
    if not key:
        return ""
    stripped = _PERIOD_PREFIX.sub("", key, count=1)
    return stripped


def _strip_except_tail(text: str) -> str:
    key = compact(text)
    if not key:
        return ""
    return _TRAILING_EXCEPT.sub("", key)


def _strip_list_suffix(key: str) -> str:
    for suffix in ("목록", "현황", "리스트"):
        if key.endswith(suffix) and len(key) > len(suffix) + 1:
            return key[: -len(suffix)]
    return key


def _axis_remainder(
    target: str,
    outputs: list[str],
    *,
    allow_raw: bool,
) -> str:
    raw = str(target or "").strip()
    key = _strip_list_suffix(compact(raw))
    if not key:
        return ""
    for item in outputs:
        axis = compact(axis_mention(item))
        if not axis:
            continue
        if key == axis:
            return ""
        if key.endswith(axis) and len(key) > len(axis) + 1:
            return key[: -len(axis)]
    if allow_raw and raw:
        return raw
    return ""


def range_slots_from_analysis(
    analysis: QueryAnalysis | None,
    query: str = "",
) -> list[RangeSlot]:
    """One mention per include/exclude slot. Do not join slots into one needle."""

    if analysis is None or meaning_failed(analysis):
        return []
    outputs = list(analysis.primary_outputs or [])
    axis = [axis_mention(item) for item in outputs]
    slots: list[RangeSlot] = []
    seen: set[tuple[str, str]] = set()

    def add(mention: str, polarity: Literal["include", "exclude"]) -> None:
        text = str(mention or "").strip()
        if polarity == "include":
            stripped = _strip_except_tail(_strip_period_prefix(text))
            if stripped:
                text = stripped
            if compact(text) in {
                compact(slot.mention) for slot in slots if slot.polarity == "exclude"
            }:
                return
        if not is_mention_needle(text):
            return
        if is_answer_axis_text(text, axis):
            return
        key = (compact(text), polarity)
        if key in seen:
            return
        seen.add(key)
        slots.append(RangeSlot(mention=text, polarity=polarity))

    blob = " ".join(
        part for part in (query, str(analysis.target or "")) if str(part).strip()
    )
    for body in _exclude_bodies(blob):
        add(body, "exclude")
    exclude_keys = {compact(slot.mention) for slot in slots if slot.polarity == "exclude"}

    include_sources = [
        _axis_remainder(
            _strip_exclude_clause(str(analysis.target or "")),
            outputs,
            allow_raw=True,
        ),
        _axis_remainder(
            _strip_exclude_clause(query),
            outputs,
            allow_raw=False,
        ),
    ]
    if "또는" in (query or "") and not _axis_remainder(
        _strip_exclude_clause(query), outputs, allow_raw=False
    ):
        include_sources.append(_strip_exclude_clause(query))
    for source in include_sources:
        parts = _or_parts(source)
        if not parts and source:
            parts = [source]
        for part in parts:
            if compact(part) in exclude_keys:
                continue
            add(part, "include")
    return slots


def range_needles_from_analysis(
    analysis: QueryAnalysis | None,
    query: str = "",
) -> list[str]:
    return [
        slot.mention
        for slot in range_slots_from_analysis(analysis, query)
        if slot.polarity == "include"
    ]


def metric_needles_from_analysis(
    analysis: QueryAnalysis | None,
    query: str = "",
) -> list[str]:
    """측정 항목은 답의 축이어도 저장소 조회에서 빼지 않는다."""

    if analysis is None or meaning_failed(analysis):
        return unique_needles(SearchMixin._question_mention_tokens(query) if query else [])
    items: list[str] = []
    metric = measure_item_surface(
        str(analysis.metric or "")
        or str(getattr(analysis.measurement, "metric", "") or "")
    )
    if metric:
        items.append(metric)
    for role in analysis.schema_roles or []:
        role_name = compact(str(role.role or ""))
        if "측정" not in role_name:
            continue
        items.extend(str(term).strip() for term in (role.search_terms or []))
    return unique_needles(items)


def filter_needles_from_analysis(
    analysis: QueryAnalysis | None,
    query: str = "",
) -> list[str]:
    return unique_needles(
        [
            *metric_needles_from_analysis(analysis, query),
            *[slot.mention for slot in range_slots_from_analysis(analysis, query)],
        ]
    )


def catalog_needles_from_analysis(
    analysis: QueryAnalysis | None,
    query: str,
) -> list[str]:
    if analysis is None or meaning_failed(analysis):
        return unique_needles(SearchMixin._question_mention_tokens(query))
    items = [
        *[axis_mention(item) for item in (analysis.primary_outputs or [])],
        *[
            slot.mention
            for slot in range_slots_from_analysis(analysis, query)
        ],
        *filter_needles_from_analysis(analysis, query),
    ]
    for role in analysis.schema_roles or []:
        items.extend(str(term).strip() for term in (role.search_terms or []))
    return unique_needles(items)


def time_role_from_procedure(procedure: str) -> str:
    if str(procedure or "").strip() == "extremum":
        return "extremum"
    return "none"


def glossary_synonyms_for_needles(
    rows: list[dict[str, Any]],
    needles: list[str],
) -> list[str]:
    wanted = {compact(item) for item in needles if compact(item)}
    if not wanted:
        return []
    extras: list[str] = []
    for row in rows:
        surfaces = [
            str(row.get("mention") or ""),
            str(row.get("surface") or ""),
            str(row.get("word_korean") or ""),
            str(row.get("standard_term") or ""),
        ]
        matched = False
        for surface in surfaces:
            key = compact(surface)
            if key and key in wanted:
                matched = True
                break
        if not matched:
            continue
        for key in (
            "word_korean",
            "standard_term",
            "mention",
            "surface",
            "abbreviation",
            "english_name",
        ):
            value = str(row.get(key) or "").strip()
            if value:
                extras.append(value)
    return unique_needles(extras)


def synonym_members_for_needles(
    groups: list[dict[str, Any]] | None,
    needles: list[str],
    *,
    limit: int = 80,
) -> list[str]:
    """Matching term-group members only. Do not cross groups."""

    wanted = {compact(item) for item in needles if compact(item)}
    if not wanted:
        return []
    extras: list[str] = []
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        if str(group.get("kind") or "term") not in {"", "term"}:
            continue
        members = [
            str(item).strip()
            for item in (group.get("members") or [])
            if str(item).strip()
        ]
        preferred = str(group.get("preferred_form") or "").strip()
        surfaces = [preferred, *members] if preferred else members
        if not any(compact(item) in wanted for item in surfaces):
            continue
        extras.extend(surfaces)
    return unique_needles(extras, limit=limit)


def answer_axis_from_analysis(analysis: QueryAnalysis | None) -> list[str]:
    if analysis is None:
        return []
    return unique_needles(list(analysis.primary_outputs or []))
