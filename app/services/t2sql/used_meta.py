"""Used-metadata slice from rewrite-before SQL + DecisionResponse."""

from __future__ import annotations

from ...schemas import (
    DecisionCandidate,
    DecisionResponse,
    ExecutionContext,
    JoinGroup,
    QueryPlan,
    T2SqlUsedMetadata,
)
from ..sql_source_qualify import extract_sql_table_refs


def empty_used_metadata() -> T2SqlUsedMetadata:
    return T2SqlUsedMetadata()


def used_metadata_for_plan(
    *,
    plan: QueryPlan | None = None,
    query_analysis=None,
    candidates: list[DecisionCandidate] | None = None,
    join_groups: list[JoinGroup] | None = None,
    resolved_entities=None,
    execution_context: ExecutionContext | None = None,
) -> T2SqlUsedMetadata:
    return T2SqlUsedMetadata(
        candidates=list(candidates or []),
        join_groups=list(join_groups or []),
        resolved_entities=list(resolved_entities or []),
        execution_context=execution_context,
        query_analysis=query_analysis,
        query_plan=plan,
        candidate_evidence=list(plan.candidate_evidence) if plan is not None else [],
    )


def _table_key(schema: str | None, table: str) -> tuple[str, str]:
    return (schema or "").lower(), table.lower()


def used_table_keys(sql: str) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for ref in extract_sql_table_refs(sql):
        keys.add(_table_key(ref.schema_name, ref.table_name))
        keys.add(_table_key(None, ref.table_name))
    return keys


def filter_used_metadata(
    decision: DecisionResponse,
    sql: str,
    *,
    include_matched_columns: bool,
    execution_context: ExecutionContext | None,
) -> T2SqlUsedMetadata:
    keys = used_table_keys(sql)
    candidates: list[DecisionCandidate] = []
    for candidate in decision.candidates:
        hit = _table_key(candidate.schema_name, candidate.table_name) in keys
        hit = hit or _table_key(None, candidate.table_name) in keys
        if not hit:
            continue
        item = candidate.model_copy(deep=True)
        if not include_matched_columns:
            item.matched_columns = []
        candidates.append(item)

    join_groups: list[JoinGroup] = []
    used_names = {table.lower() for _, table in keys}
    for group in decision.join_groups:
        member_names = {str(member.table_name).lower() for member in group.members}
        if member_names & used_names:
            join_groups.append(group)

    plan = decision.query_plan

    allowed = sorted({table for _, table in keys if table})
    public_ec = execution_context
    if public_ec is not None:
        public_ec = public_ec.model_copy(update={"allowed_objects": allowed})

    entity_tables = used_names
    entities = [
        entity
        for entity in decision.resolved_entities
        if str(entity.table).lower() in entity_tables
    ]
    return T2SqlUsedMetadata(
        candidates=candidates,
        join_groups=join_groups,
        resolved_entities=entities,
        execution_context=public_ec,
        query_analysis=decision.query_analysis,
        query_plan=plan,
        candidate_evidence=list(plan.candidate_evidence) if plan is not None else [],
    )
