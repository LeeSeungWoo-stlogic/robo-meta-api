"""Bind confirm_intent to query_plan SoT. Do not re-try resolved codes."""

from __future__ import annotations

from typing import Any

from ...schemas import QueryAnalysis, QueryPlan, ResolvedEntity
from ..decision_postgres.store_first import is_fact_unresolved_reason

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
_AGG_TOKENS = ("집계", "aggregation", "합계", "총합")
_FACT_SLOT_TOKENS = ("팩트", "fact")


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


def should_skip_confirm(plan: QueryPlan | None) -> bool:
    """Resolved plan filters are SoT. Do not ask the confirm LLM to reject them."""

    if plan is None:
        return False
    return bool(resolved_plan_values(plan))


def _slot_is_requested_schema_role(
    slot: str,
    *,
    analysis: QueryAnalysis | None,
    plan: QueryPlan | None,
) -> bool:
    """Confirm must not reject table roles the analyzer/plan already asked for."""

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
    return any(
        name and (name == text or name in text or text in name) for name in names
    )


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


def align_entities_to_plan(
    entities: list[ResolvedEntity],
    plan: QueryPlan | None,
) -> list[ResolvedEntity]:
    """Keep only entity codes that the plan already chose. Do not invent codes."""

    used = set(resolved_plan_values(plan))
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


def reconcile_confirm(
    confirm: dict[str, Any],
    *,
    plan: QueryPlan | None,
    analysis: QueryAnalysis | None,
) -> dict[str, Any]:
    """Drop missing slots that SoT already closed, or that we must not invent.

    Resolved filters close code/entity slots. A missing period is not a
    reject reason: generate may use default_date_column or omit the date.
    """
    missing = _missing_slots(confirm)
    if bool(confirm.get("accept")) and not missing:
        return {"accept": True, "missing": []}

    resolved = resolved_plan_values(plan)
    aggregation = ""
    if analysis is not None and analysis.measurement is not None:
        aggregation = str(analysis.measurement.aggregation or "").strip()

    kept: list[str] = []
    for slot in missing:
        text = slot.casefold()
        if any(token in text for token in _PERIOD_TOKENS):
            continue
        if any(token in text for token in _CODE_TOKENS) and resolved:
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
    return {"accept": not kept, "missing": kept}
