"""Record match type, mention, field, score, and select/reject reasons."""

from __future__ import annotations

from typing import Any, Iterable

from ...schemas import CandidateEvidence
from .helpers import _resolve_subject_area
from .store_first import table_blob
from .table_type import list_table_type

_FACT_LIKE = frozenset({"Fact", "Raw"})
_GRAIN_SURFACE = {
    "month": "월",
    "day": "일",
    "hour": "시간",
    "instant": "실시간",
}


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _table_id(row: dict[str, Any]) -> int | None:
    raw = row.get("id")
    if raw is None:
        raw = row.get("table_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _table_name(row: dict[str, Any]) -> str | None:
    return _text(row.get("original_name") or row.get("name") or row.get("table_name"))


def _schema_name(row: dict[str, Any]) -> str | None:
    return _text(row.get("schema_name"))


def _score(row: dict[str, Any], default: float) -> float:
    try:
        return float(row.get("score") if row.get("score") is not None else default)
    except (TypeError, ValueError):
        return default


def _mapping_item(
    row: dict[str, Any],
    *,
    selected: bool,
    reason: str,
) -> CandidateEvidence:
    return CandidateEvidence(
        kind="value_mapping",
        mention=_text(row.get("matched_mention") or row.get("natural_value")),
        match_type=_text(row.get("match_type")) or "exact",
        matched_field=_text(row.get("matched_field")) or "natural_value",
        score=_score(row, 1.0 if selected else 0.0),
        selected=selected,
        reason=reason,
        table_id=_table_id(row),
        table_name=_table_name(row),
        schema_name=_schema_name(row),
        code_value=_text(row.get("code_value")),
        column_fqn=_text(row.get("column_fqn")),
    )


def _catalog_item(
    table: dict[str, Any],
    *,
    selected: bool,
    reason: str,
) -> CandidateEvidence:
    return CandidateEvidence(
        kind="catalog",
        mention=_text(table.get("matched_mention")),
        match_type=_text(table.get("match_type")) or "mention",
        matched_field=_text(table.get("matched_field")),
        score=_score(table, 1.0 if selected else 0.0),
        selected=selected,
        reason=reason,
        table_id=_table_id(table),
        table_name=_table_name(table),
        schema_name=_schema_name(table),
    )


def _grain_surface(grain: str | None) -> str | None:
    if not grain:
        return None
    return _GRAIN_SURFACE.get(str(grain), str(grain) or None)


def _fact_item(
    table: dict[str, Any],
    *,
    selected: bool,
    reason: str,
    grain: str | None = None,
) -> CandidateEvidence:
    surface = _grain_surface(grain)
    blob = table_blob(table)
    matched_field = None
    match_type = "fact"
    if surface and surface in blob:
        match_type = "grain"
        if surface in str(table.get("description") or ""):
            matched_field = "description"
        elif surface in str(table.get("logical_name") or ""):
            matched_field = "logical_name"
        else:
            matched_field = "analyzed_description"
    elif list_table_type(_resolve_subject_area(table)) == "Fact":
        match_type = "fact_type"
        matched_field = "subject_area"
    hits = 1.0 if surface and surface in blob else 0.0
    return CandidateEvidence(
        kind="fact",
        mention=surface,
        match_type=match_type,
        matched_field=matched_field,
        score=hits,
        selected=selected,
        reason=reason,
        table_id=_table_id(table),
        table_name=_table_name(table),
        schema_name=_schema_name(table),
    )


def _dedupe(items: list[CandidateEvidence]) -> list[CandidateEvidence]:
    seen: set[tuple[Any, ...]] = set()
    kept: list[CandidateEvidence] = []
    for item in items:
        key = (
            item.kind,
            item.table_id,
            item.table_name,
            item.column_fqn,
            item.code_value,
            item.mention,
            item.selected,
            item.reason,
        )
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept


def build_candidate_evidence(
    *,
    selected_mappings: Iterable[dict[str, Any]] | None = None,
    held_mappings: Iterable[dict[str, Any]] | None = None,
    source_dropped_mappings: Iterable[dict[str, Any]] | None = None,
    recall_mappings: Iterable[dict[str, Any]] | None = None,
    catalog_tables: Iterable[dict[str, Any]] | None = None,
    fact_tables: Iterable[dict[str, Any]] | None = None,
    selected_ids: set[int] | None = None,
    mapped_ids: set[int] | None = None,
    group_ids: set[int] | None = None,
    location_ids: set[int] | None = None,
    chosen_fact_ids: set[int] | None = None,
    dropped_unjoinable_ids: set[int] | None = None,
    query_requests_fact: bool = False,
    stay_on_selected: bool = False,
    fact_unresolved: str | None = None,
    grain: str | None = None,
) -> list[CandidateEvidence]:
    selected = {int(item) for item in (selected_ids or set())}
    mapped = {int(item) for item in (mapped_ids or set())}
    groups = {int(item) for item in (group_ids or set())}
    locations = {int(item) for item in (location_ids or set())}
    chosen_facts = {int(item) for item in (chosen_fact_ids or set())}
    unjoinable = {int(item) for item in (dropped_unjoinable_ids or set())}
    items: list[CandidateEvidence] = []

    for row in selected_mappings or []:
        items.append(
            _mapping_item(row, selected=True, reason="유형 사전 통과 후 값 코드 바인딩")
        )
    for row in held_mappings or []:
        items.append(
            _mapping_item(
                row,
                selected=False,
                reason="같은 멘션이 여러 컬럼에 걸려 보류",
            )
        )
    for row in source_dropped_mappings or []:
        items.append(
            _mapping_item(
                row,
                selected=False,
                reason="다른 source_instance 값매핑이라 제외",
            )
        )
    for row in recall_mappings or []:
        items.append(
            _mapping_item(
                row,
                selected=False,
                reason="의미 검색 후보, 승인 값매핑이 아님",
            )
        )

    for table in catalog_tables or []:
        table_id = _table_id(table)
        taken = table_id is not None and int(table_id) in selected
        if taken:
            if table_id in mapped:
                reason = "값매핑이 붙은 표"
            elif table_id in groups:
                reason = "질문 축 카탈로그 적중(그룹 차원)"
            elif table_id in locations:
                reason = "답 축 위치 마스터"
            else:
                reason = "카탈로그 멘션 적중"
        elif table_id is not None and int(table_id) in unjoinable:
            reason = "팩트와 승인 JOIN 경로가 없음"
        elif stay_on_selected:
            reason = "목록 질의에서 선정되지 않은 표"
        else:
            reason = "카탈로그 적중했으나 계획 시드에 채택되지 않음"
        items.append(_catalog_item(table, selected=taken, reason=reason))

    fact_like: list[dict[str, Any]] = []
    seen_facts: set[int] = set()
    for table in fact_tables or []:
        if list_table_type(_resolve_subject_area(table)) not in _FACT_LIKE:
            continue
        table_id = _table_id(table)
        if table_id is None or int(table_id) in seen_facts:
            continue
        seen_facts.add(int(table_id))
        fact_like.append(table)

    if not query_requests_fact:
        if fact_like:
            items.append(
                CandidateEvidence(
                    kind="fact",
                    mention=None,
                    match_type="skipped",
                    matched_field=None,
                    score=0.0,
                    selected=False,
                    reason=f"목록·비측정 질의라 팩트 {len(fact_like)}개를 계획에 넣지 않음",
                )
            )
        return _dedupe(items)

    for table in fact_like:
        table_id = _table_id(table)
        assert table_id is not None
        taken = int(table_id) in chosen_facts and int(table_id) in selected
        if taken:
            reason = "채택된 팩트"
            if grain:
                surface = _grain_surface(grain)
                if surface and surface in table_blob(table):
                    reason = "입도 힌트가 논리명·설명에 맞는 팩트"
        elif fact_unresolved:
            reason = fact_unresolved
        else:
            reason = "채택된 팩트가 아니라 제외"
        items.append(
            _fact_item(table, selected=taken, reason=reason, grain=grain)
        )

    return _dedupe(items)
