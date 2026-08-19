"""Bind confirm_intent to query_plan SoT. Do not re-try resolved codes."""

from __future__ import annotations

import re
from typing import Any

from ...schemas import QueryAnalysis, QueryPlan, ResolvedEntity
from ..metadata_repository._search import SearchMixin
from ..decision_postgres.aliases import peel_type_suffix, type_product_surfaces
from ..decision_postgres.store_first import (
    is_fact_unresolved_reason,
    is_range_code_unresolved_reason,
)

_CODE_TOKENS = (
    "코드",
    "code",
    "facility",
    "엔티티",
    "entity",
)
_PERIOD_TOKENS = (
    "기간",
    "날짜",
    "period",
    "date",
    "일자",
    "연도",
    "연월",
    "year",
    "month",
    "월",
)
_PERIOD_FILTER_TOKENS = (
    "기간",
    "날짜",
    "period",
    "date",
    "일자",
    "연도",
    "연월",
)
_AGG_TOKENS = ("집계", "aggregation", "합계", "총합")
_FACT_SLOT_TOKENS = ("팩트", "fact")
_IN_CONFLICT_TOKENS = ("충돌", "여러 코드", "복수 코드", "코드가 여러")
_TYPE_TOKENS = ("유형", "타입", "type")


def _missing_slots(confirm: dict[str, Any]) -> list[str]:
    raw = confirm.get("missing")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip() for part in raw.replace("，", ",").split(",") if part.strip()]
    slots: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text:
            slots.append(text)
    return slots


def fact_left_unresolved(plan: QueryPlan | None) -> bool:
    if plan is None:
        return False
    return any(
        is_fact_unresolved_reason(str(item))
        for item in (plan.unresolved_requirements or [])
    )


_PAREN_TAIL = re.compile(r"\([^)]*\)$")


def range_code_left_unresolved(plan: QueryPlan | None) -> bool:
    if plan is None:
        return False
    flagged = any(
        is_range_code_unresolved_reason(str(item))
        for item in (plan.unresolved_requirements or [])
    )
    if not flagged:
        return False
    if resolved_code_filter_values(plan):
        return False
    return True


def _alias_key(text: str) -> str:
    return _PAREN_TAIL.sub("", SearchMixin._compact_natural_text(text))


def _mention_covered_by_question(mention: str, blob: str) -> bool:
    key = _alias_key(mention)
    if not key:
        return False
    if key in blob:
        return True
    peeled = peel_type_suffix(key)
    if peeled is None or not peeled[0]:
        return False
    instance, group = peeled
    return any(
        SearchMixin._compact_natural_text(surface) in blob
        for surface in type_product_surfaces(instance, group)
    )


def should_skip_confirm(
    plan: QueryPlan | None,
    analysis: QueryAnalysis | None = None,
) -> bool:
    """Store 교차 선택과 SQL 생성이 본 경로다. confirm으로 다시 재판하지 않는다."""

    del plan, analysis
    return True


def _slot_is_requested_schema_role(
    slot: str,
    *,
    analysis: QueryAnalysis | None,
    plan: QueryPlan | None,
) -> bool:
    """Close role-name missing slots. Do not close value/range slots by substring."""

    text = slot.casefold()
    if "마스터" in text:
        return True
    names: list[str] = []
    if analysis is not None:
        names.extend(str(role.role or "").casefold() for role in analysis.schema_roles)
    if plan is not None:
        for table in plan.required_tables:
            names.append(str(table.role or "").casefold())
            names.extend(str(item).casefold() for item in table.roles or [])
    return any(name and name == text for name in names)


def resolved_plan_values(plan: QueryPlan | None) -> list[str]:
    if plan is None:
        return []
    values: list[str] = []
    for item in plan.filters:
        if item.resolution_status != "resolved":
            continue
        text = str(item.value or "").strip()
        if text:
            values.append(text)
    return values


def _filter_meaning_is_period(meaning: str) -> bool:
    text = str(meaning or "").casefold()
    if "측정 기간" in text:
        return True
    return any(token in text for token in _PERIOD_FILTER_TOKENS)


def resolved_code_filter_values(plan: QueryPlan | None) -> list[str]:
    if plan is None:
        return []
    values: list[str] = []
    for item in plan.filters:
        if item.resolution_status != "resolved":
            continue
        if _filter_meaning_is_period(str(item.meaning or "")):
            continue
        text = str(item.value or "").strip()
        if text:
            values.append(text)
    return values


def _split_codes(value: str) -> list[str]:
    return [
        part.strip().strip("'\"")
        for part in str(value or "").replace("，", ",").split(",")
        if part.strip().strip("'\"")
    ]


def _code_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return _split_codes(raw)
    codes: list[str] = []
    for item in raw:
        codes.extend(_split_codes(str(item)))
    return codes


def plan_code_set(plan: QueryPlan | None) -> set[str]:
    codes: set[str] = set()
    for value in resolved_code_filter_values(plan):
        codes.update(_split_codes(value))
    return codes


def _codes_by_column(plan: QueryPlan | None) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    if plan is None:
        return grouped
    for item in plan.filters:
        if item.resolution_status != "resolved":
            continue
        if _filter_meaning_is_period(str(item.meaning or "")):
            continue
        column = str(item.column or "").strip().casefold()
        if not column:
            continue
        grouped.setdefault(column, []).extend(_split_codes(str(item.value or "")))
    return grouped


def _in_same_column(plan: QueryPlan | None, code: str) -> bool:
    for codes in _codes_by_column(plan).values():
        if code in codes and len(set(codes)) > 1:
            return True
    return False


def align_entities_to_plan(
    entities: list[ResolvedEntity],
    plan: QueryPlan | None,
) -> list[ResolvedEntity]:
    """Keep only entity codes that the plan already chose. Do not invent codes."""

    used = plan_code_set(plan)
    if not used:
        return list(entities)
    aligned: list[ResolvedEntity] = []
    for entity in entities:
        if not entity.values:
            aligned.append(entity)
            continue
        matched = [item for item in entity.values if str(item.code or "") in used]
        aligned.append(
            entity.model_copy(update={"values": matched}) if matched else entity
        )
    return aligned


def _question_blob(query: str | None, analysis: QueryAnalysis | None) -> str:
    parts = [str(query or "")]
    if analysis is not None:
        parts.extend(
            [
                str(analysis.target or ""),
                str(analysis.metric or ""),
                *[str(item) for item in (analysis.entities_include or [])],
                *[str(item) for item in (analysis.primary_outputs or [])],
            ]
        )
    return SearchMixin._compact_natural_text(" ".join(parts))


def confirm_plan_bindings(
    *,
    plan: QueryPlan | None,
    analysis: QueryAnalysis | None,
    entities: list[ResolvedEntity] | None = None,
    query: str | None = None,
) -> list[str]:
    """Mention, type, table, and metric checks. Same-column IN is not a conflict."""

    issues: list[str] = []
    if plan is None:
        return issues
    blob = _question_blob(query, analysis)
    plan_codes = plan_code_set(plan)
    table_names = {
        str(table.table_name or "").casefold()
        for table in plan.required_tables
        if str(table.table_name or "").strip()
    }
    for entity in entities or []:
        mention = SearchMixin._compact_natural_text(str(entity.mention or ""))
        entity_codes = [
            str(item.code or "").strip()
            for item in (entity.values or [])
            if str(item.code or "").strip()
        ]
        if mention and blob and not _mention_covered_by_question(mention, blob) and any(
            code in plan_codes for code in entity_codes
        ):
            issues.append(f"언급과 값매핑이 맞지 않음:{entity.mention}")
        entity_table = str(entity.table or "").casefold()
        if (
            entity_table
            and table_names
            and entity_table not in table_names
            and any(code in plan_codes for code in entity_codes)
        ):
            column_hit = any(
                entity_table in column
                for column in _codes_by_column(plan)
            )
            if not column_hit:
                issues.append(f"값매핑 표가 계획에 없음:{entity.table}")
    aggregation = plan.aggregation
    if aggregation is not None and str(aggregation.value_column or "").strip():
        value_column = str(aggregation.value_column).strip()
        required = [
            column
            for table in plan.required_tables
            for column in (table.required_columns or [])
        ]
        if required and value_column not in required:
            issues.append("metric이 fact 컬럼과 맞지 않음")
    return issues


def reconcile_confirm(
    confirm: dict[str, Any],
    *,
    plan: QueryPlan | None,
    analysis: QueryAnalysis | None,
    entities: list[ResolvedEntity] | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """Drop missing slots that SoT already closed, or that we must not invent.

    Same-column IN codes are accepted. Extra add_codes and typed drop_codes
    that do not match the slot become ENTITY_UNRESOLVED.
    """
    missing = _missing_slots(confirm)
    resolved_codes = resolved_code_filter_values(plan)
    plan_codes = plan_code_set(plan)
    aggregation = ""
    if analysis is not None and analysis.measurement is not None:
        aggregation = str(analysis.measurement.aggregation or "").strip()

    kept: list[str] = []
    for slot in missing:
        text = slot.casefold()
        if any(token in text for token in _PERIOD_TOKENS):
            continue
        if any(token in text for token in _IN_CONFLICT_TOKENS) and any(
            len(set(codes)) > 1 for codes in _codes_by_column(plan).values()
        ):
            continue
        if any(token in text for token in _CODE_TOKENS) and resolved_codes:
            continue
        if any(token in text for token in _AGG_TOKENS) and aggregation:
            continue
        if any(token in text for token in _FACT_SLOT_TOKENS) and fact_left_unresolved(
            plan
        ):
            continue
        if _slot_is_requested_schema_role(slot, analysis=analysis, plan=plan):
            continue
        kept.append(slot)

    extra_codes = [
        code for code in _code_list(confirm.get("add_codes")) if code not in plan_codes
    ]
    if extra_codes:
        kept.append("계획에 없는 코드:" + ",".join(extra_codes))

    drop_codes = _code_list(confirm.get("drop_codes"))
    typed_drops: list[str] = []
    for code in drop_codes:
        if code not in plan_codes:
            continue
        if _in_same_column(plan, code):
            continue
        if any(token in slot.casefold() for slot in missing for token in _TYPE_TOKENS):
            typed_drops.append(code)
    if typed_drops:
        kept.append("슬롯 유형과 다른 코드:" + ",".join(typed_drops))

    kept.extend(
        confirm_plan_bindings(
            plan=plan,
            analysis=analysis,
            entities=entities,
            query=query,
        )
    )
    return {"accept": not kept, "missing": kept}
