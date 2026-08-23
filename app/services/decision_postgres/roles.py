from __future__ import annotations

import re
from typing import Any

from ...schemas import QueryAnalysis, SchemaRoleRequirement
from .grain import (
    fact_role_for_grain,
    grain_from_fact_role,
    is_measurement_role,
    is_period_fact_role,
    period_fact_candidate_allowed,
    resolve_time_grain,
)
from .helpers import _resolve_subject_area
from .table_type import table_type_allows_role


def _role_blob(analysis: QueryAnalysis) -> str:
    return " ".join(
        f"{role.role} {' '.join(role.search_terms)}"
        for role in analysis.schema_roles
    )


def _role_covers(role: SchemaRoleRequirement, *terms: str, exclude: tuple[str, ...] = ()) -> bool:
    text = f"{role.role} {' '.join(role.search_terms)}".casefold()
    if exclude and any(token in text for token in exclude):
        return False
    return any(term in text for term in terms)


def _enrich_analysis_roles(query: str, analysis: QueryAnalysis) -> QueryAnalysis:
    """No domain-list role injection. Store hits drive tables, not this hook."""
    return analysis


def _is_dimension_facility_query(query: str, analysis: QueryAnalysis) -> bool:
    """조직/시설 개수·목록 질의 — hist 시계열 단독 상위 억제 대상."""
    q = (query or "").casefold()
    goal = (analysis.goal or "").casefold()
    blob = f"{q} {goal}"
    dimension_hits = ("개수", "목록", "몇 개", "몇개", "리스트", "어디")
    measure_hits = ("계측", "측정", "평균", "합계", "값", "잔류", "수질", "태그값")
    if not any(term in blob for term in dimension_hits):
        return False
    if any(term in blob for term in measure_hits):
        return False
    role_blob = " ".join(
        f"{role.role} {' '.join(role.search_terms)}"
        for role in analysis.schema_roles
    ).casefold()
    hub_hits = ("사업장", "정수장", "본부", "조직", "시설", "마스터")
    return any(term in role_blob or term in blob for term in hub_hits)


def _apply_subject_area_ranking(
    tables: list[dict[str, Any]],
    *,
    dimension_facility: bool,
) -> list[dict[str, Any]]:
    """Prefer master/code hubs on dimension queries; demote hist/link hijacks."""
    if not tables or not dimension_facility:
        return tables
    adjusted: list[dict[str, Any]] = []
    for table in tables:
        item = dict(table)
        area = _resolve_subject_area(item)
        score = float(item.get("score") or 0.0)
        if area in {"hist", "link"}:
            score *= 0.35
        elif area == "master":
            score = min(1.0, score + 0.12)
        elif area == "code":
            score = min(1.0, score + 0.04)
        item["score"] = score
        item["subject_area"] = area
        adjusted.append(item)
    adjusted.sort(
        key=lambda item: (
            -float(item.get("score") or 0.0),
            str(item.get("original_name") or item.get("name") or "").lower(),
        )
    )
    return adjusted


def _role_candidate_score(
    role: SchemaRoleRequirement,
    table: dict[str, Any],
) -> float:
    base = float(table.get("score") or 0.0)
    role_text = " ".join([role.role, *role.search_terms]).lower()
    table_name = str(
        table.get("original_name") or table.get("name") or ""
    ).lower()
    table_text = " ".join(
        [
            table_name,
            str(table.get("description") or "").lower(),
            str(table.get("analyzed_description") or "").lower(),
        ]
    )
    tokens = {
        token
        for token in re.findall(r"[가-힣]{2,}|[a-z0-9_]{3,}", role_text)
        if token not in {"데이터", "테이블", "정보"}
    }
    matched = sum(1 for token in tokens if token in table_text)
    lexical = min(0.3, matched * 0.08)
    temporal_signals = (
        (("15분", "십오분"), ("15분", "15mi")),
        (("시간별", "시간 단위", "매시간", "01hh"), ("시간별", "시간 단위", "01hh")),
        (("일 단위", "일별", "하루", "일자", "01dd"), ("일 단위", "일별", "하루", "일자", "01dd")),
        (("월별", "월 단위", "한달", "01mm"), ("월별", "월 단위", "01mm")),
    )
    for role_terms, table_terms in temporal_signals:
        if any(term in role_text for term in role_terms) and any(
            term in table_text for term in table_terms
        ):
            lexical = max(lexical, 0.45)
    area = _resolve_subject_area(table)
    hub_role = any(
        term in role_text
        for term in ("사업장", "정수장", "본부", "조직", "시설", "마스터", "코드")
    )
    if hub_role and area in {"hist", "link"}:
        return max(0.0, min(1.0, base + lexical) * 0.35)
    if hub_role and area == "master":
        lexical = max(lexical, 0.2)
    score = min(1.0, base + lexical)
    if is_period_fact_role(role) and grain_from_fact_role(role) != "instant":
        if area == "agg":
            score = min(1.0, score + 0.12)
        elif area in {"raw", "hist", "link"}:
            score *= 0.25
    return score


def _role_candidate_has_evidence(
    role: SchemaRoleRequirement,
    table: dict[str, Any],
) -> bool:
    role_text = " ".join([role.role, *role.search_terms]).lower()
    table_text = " ".join(
        [
            str(table.get("original_name") or table.get("name") or "").lower(),
            str(table.get("description") or "").lower(),
            str(table.get("analyzed_description") or "").lower(),
        ]
    )
    tokens = {
        token
        for token in re.findall(r"[가-힣]{2,}|[a-z0-9_]{3,}", role_text)
        if token not in {"데이터", "테이블", "정보", "마스터"}
    }
    if any(token in table_text for token in tokens):
        return True
    temporal_signals = (
        (("15분", "십오분"), ("15분", "15mi")),
        (("시간별", "시간 단위", "매시간", "01hh"), ("시간별", "시간 단위", "01hh")),
        (("일 단위", "일별", "하루", "일자", "01dd"), ("일 단위", "일별", "하루", "일자", "01dd")),
        (("월별", "월 단위", "한달", "01mm"), ("월별", "월 단위", "01mm")),
    )
    if any(
        any(term in role_text for term in role_terms)
        and any(term in table_text for term in table_terms)
        for role_terms, table_terms in temporal_signals
    ):
        return True
    area = _resolve_subject_area(table)
    if area == "agg" and any(
        term in role_text for term in ("계측", "측정", "시계열", "팩트", "값", "데이터")
    ):
        return True
    if area == "master" and any(
        term in role_text
        for term in ("사업장", "정수장", "본부", "태그", "마스터", "시설")
    ):
        return True
    return False


def prepare_role_candidate_rows(
    role: SchemaRoleRequirement,
    rows: list[dict[str, Any]],
    *,
    semantic_floor: float,
    min_score_ratio: float,
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for row in rows:
        item = {
            **row,
            "vector_score": float(row.get("score") or 0.0),
            "score": _role_candidate_score(role, row),
            "role_evidence": _role_candidate_has_evidence(role, row),
        }
        area = _resolve_subject_area(item)
        role_text = " ".join([role.role, *role.search_terms])
        if not table_type_allows_role(role_text, area):
            continue
        if not period_fact_candidate_allowed(role, item, subject_area=area):
            continue
        if item["role_evidence"] or float(item["vector_score"]) >= semantic_floor:
            prepared.append(item)
    prepared.sort(
        key=lambda row: (
            -float(row.get("score") or 0.0),
            str(row.get("original_name") or row.get("name") or ""),
        )
    )
    if not prepared:
        return prepared
    cutoff = float(prepared[0].get("score") or 0.0) * min_score_ratio
    return [
        row
        for row in prepared
        if float(row.get("score") or 0.0) >= cutoff
    ]


def backfill_empty_role_candidates(
    role_candidates: dict[str, list[dict[str, Any]]],
    roles: list[SchemaRoleRequirement],
    pool: list[dict[str, Any]],
    *,
    semantic_floor: float,
    min_score_ratio: float,
) -> dict[str, list[dict[str, Any]]]:
    """If a role search returned only wrong types, reuse allowed tables already in pool.

    Does not invent a table. Physical names are not used.
    """
    filled = {key: list(value) for key, value in role_candidates.items()}
    if not pool:
        return filled
    for role in roles:
        if filled.get(role.role):
            continue
        rows = prepare_role_candidate_rows(
            role,
            pool,
            semantic_floor=semantic_floor,
            min_score_ratio=min_score_ratio,
        )
        if rows:
            filled[role.role] = rows
    return filled
