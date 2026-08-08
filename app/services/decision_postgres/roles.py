from __future__ import annotations

import re
from typing import Any

from ...schemas import JoinRequirement, QueryAnalysis, SchemaRoleRequirement
from .helpers import _resolve_subject_area


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
    """Fill missing facility/tag/fact roles HyDE often collapses into one blob."""
    if analysis.status != "complete":
        return analysis
    q = query or ""
    roles = list(analysis.schema_roles)
    joins = list(analysis.join_requirements)
    measure_exclude = ("계측", "측정", "시계열", "팩트", "값", "현황", "데이터")

    def _covered(*terms: str, exclude: tuple[str, ...] = ()) -> bool:
        return any(_role_covers(role, *terms, exclude=exclude) for role in roles)

    def _add(role: str, terms: list[str], *, necessity: str = "required") -> None:
        if any(role == existing.role for existing in roles):
            return
        roles.append(
            SchemaRoleRequirement(
                role=role,
                necessity=necessity,  # type: ignore[arg-type]
                cardinality="many",
                search_terms=terms,
            )
        )

    plant = any(term in q for term in ("정수장", "사업장"))
    measure = any(term in q for term in ("계측", "측정", "값", "현황", "데이터"))
    region = any(term in q for term in ("충청", "금강", "한강", "낙동", "영섬", "본부", "권역", "지역"))
    inventory = any(term in q for term in ("어떤", "무엇", "항목", "데이터들"))
    timeseries = any(
        term in q for term in ("계측값", "현황", "평균", "합계", "년", "월", "일", "시간")
    ) and not inventory

    # "사업장 계측 데이터" 같은 붕괴 역할은 사업장 마스터 커버로 치지 않는다.
    if plant and not _covered("사업장", "정수장", "시설", exclude=measure_exclude):
        _add("사업장 마스터", ["사업장", "정수장", "SUJ", "사업장코드", "사업장이름", "RDISAUP"])
    if region and not _covered("본부", "권역", "유역", "지역본부", exclude=measure_exclude):
        _add("지역본부 마스터", ["본부", "지역본부", "BNB", "유역본부", "권역", "RDIBONBU"])
    if measure and plant and not _covered("태그", "측정항목", "태그마스터", exclude=("계측값", "시계열")):
        _add("태그 마스터", ["태그", "측정항목", "TAG", "TAGSN", "태그명", "RDITAG"])
    if timeseries and not _covered("일별", "월별", "시간별", "01dd", "01mm", "01hh", "팩트"):
        _add("일별 계측 팩트", ["일별", "일 단위", "계측값", "01DD", "LOG_TIME", "VAL", "RDD01DD"])

    role_names = {role.role for role in roles}
    def _link(a: str, b: str, keys: list[str]) -> None:
        if a in role_names and b in role_names:
            if any({j.from_role, j.to_role} == {a, b} for j in joins):
                return
            joins.append(
                JoinRequirement(
                    from_role=a,
                    to_role=b,
                    required=True,
                    key_meanings=keys,
                )
            )

    if "사업장 마스터" in role_names and "지역본부 마스터" in role_names:
        _link("사업장 마스터", "지역본부 마스터", ["본부코드", "BNB_CODE"])
    if "사업장 마스터" in role_names and "태그 마스터" in role_names:
        _link("사업장 마스터", "태그 마스터", ["사업장코드", "SUJ_CODE"])
    if "태그 마스터" in role_names and "일별 계측 팩트" in role_names:
        _link("태그 마스터", "일별 계측 팩트", ["태그일련번호", "TAGSN"])
    if "사업장 마스터" in role_names and "일별 계측 팩트" in role_names:
        _link("사업장 마스터", "일별 계측 팩트", ["사업장코드", "SUJ_CODE"])

    # Remove HyDE hybrid roles that collapse facility+measure into one seed.
    concrete = {"사업장 마스터", "지역본부 마스터", "태그 마스터", "일별 계측 팩트"}
    if concrete & {role.role for role in roles}:
        roles = [
            role
            for role in roles
            if role.role in concrete
            or not (
                _role_covers(role, "사업장", "정수장", "시설")
                and _role_covers(role, "계측", "측정", "데이터", "값", "현황")
            )
        ]

    analysis.schema_roles = roles[:10]
    kept = {role.role for role in analysis.schema_roles}
    analysis.join_requirements = [
        join
        for join in joins
        if join.from_role in kept and join.to_role in kept
    ][:10]
    return analysis


def _is_dimension_facility_query(query: str, analysis: QueryAnalysis) -> bool:
    """조직/시설 개수·목록 질의 — hist 시계열 단독 상위 억제 대상."""
    q = (query or "").casefold()
    intent = (analysis.intent or "").casefold()
    blob = f"{q} {intent}"
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
    # Prefer exact physical hub tokens injected by role enrichment.
    for hub_token, hub_names in (
        ("rdisaup", ("rdisaup_tb",)),
        ("rdibonbu", ("rdibonbu_tb",)),
        ("rditag", ("rditag_tb",)),
        ("rdd01dd", ("rdd01dd_tb",)),
    ):
        if hub_token in role_text and any(name in table_name for name in hub_names):
            lexical = max(lexical, 0.55)
    temporal_signals = (
        (("15분", "십오분"), ("15분", "15mi")),
        (("시간별", "시간 단위", "매시간"), ("시간별", "시간 단위", "01hh")),
        (("일 단위", "일별", "하루"), ("일 단위", "일별", "01dd")),
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
    return min(1.0, base + lexical)


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
        (("시간별", "시간 단위", "매시간"), ("시간별", "시간 단위", "01hh")),
        (("일 단위", "일별", "하루"), ("일 단위", "일별", "01dd")),
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
