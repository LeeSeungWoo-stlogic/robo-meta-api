from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..runtime_config import get_runtime
from .embedding_provider import get_embedding_provider
from ..schemas import (
    DecisionCandidate,
    DecisionResponse,
    ExecutionContext,
    JoinBridge,
    JoinGroup,
    MatchedColumn,
    ResolvedEntity,
    ResolvedValue,
    TableKey,
)
from .metadata_repository import PostgresMetadataRepository


async def _embed_question(question: str) -> list[float]:
    """v1 경로 — EmbeddingProvider 인터페이스로 위임 (기본은 기존과 동일한 HTTP)."""
    return await get_embedding_provider().embed(question)


def _candidate(
    table: dict[str, Any],
    columns: list[dict[str, Any]],
    *,
    source: str,
) -> DecisionCandidate:
    matched = [
        MatchedColumn(
            column_name=str(column["name"]),
            score=float(column.get("score") or 0.0),
            constraints=["PK"] if column.get("is_primary_key") else [],
            column_name_kr=(column.get("description") or None),
            data_type=(column.get("dtype") or None),
            description=(
                column.get("analyzed_description")
                or column.get("description")
                or None
            ),
        )
        for column in columns
    ]
    return DecisionCandidate(
        db=str(table.get("db") or ""),
        schema_name=str(table.get("schema_name") or ""),
        table_name=str(table.get("original_name") or table.get("name") or ""),
        score=float(table.get("score") or 0.0),
        source=source,
        target_class="source",
        subject_area="unknown",
        matched_columns=matched,
    )


def _resolved_entities(mappings: list[dict[str, Any]]) -> list[ResolvedEntity]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for mapping in mappings:
        key = (
            str(mapping.get("natural_value") or ""),
            str(mapping.get("db") or ""),
            str(mapping.get("schema_name") or ""),
            str(mapping.get("table_name") or ""),
        )
        grouped[key].append(mapping)
    entities: list[ResolvedEntity] = []
    for (mention, db, schema_name, table_name), rows in grouped.items():
        entities.append(
            ResolvedEntity(
                mention=mention,
                entity_type="code",
                db=db or None,
                schema_name=schema_name or None,
                table=table_name,
                name_column=str(rows[0].get("column_name") or ""),
                code_column=str(rows[0].get("column_name") or "") or None,
                values=[
                    ResolvedValue(
                        code=str(row.get("code_value") or ""),
                        confidence=1.0,
                    )
                    for row in rows
                ],
                source="value_examples",
            )
        )
    return entities


async def decide(
    repository: PostgresMetadataRepository,
    *,
    query: str,
    include_matched_columns: bool,
    column_top_m: int | None,
    auto_resolve_entities: bool,
    table_limit: int | None = None,
) -> DecisionResponse:
    runtime = get_runtime()
    # 요청 table_limit 하나로 검색 top-k와 최종 cap을 함께 제어 (v0.7 계약,
    # decision_service._resolve_decision_policy와 동일 의미)
    effective_top_k = (
        max(1, min(50, int(table_limit)))
        if table_limit is not None
        else runtime.decision.table_top_k
    )
    embedding = await _embed_question(query)
    tables = await repository.search_tables(
        embedding,
        limit=effective_top_k,
    )
    mappings = await repository.find_value_mappings(query)
    mapped_table_ids = {
        int(mapping["table_id"])
        for mapping in mappings
        if mapping.get("table_id") is not None
    }
    existing_ids = {int(table["id"]) for table in tables}
    for table in await repository.fetch_tables_by_ids(mapped_table_ids - existing_ids):
        table["score"] = 1.0
        tables.insert(0, table)
    neighbor_ids = await repository.fk_neighbor_table_ids(mapped_table_ids)
    current_ids = {int(table["id"]) for table in tables}
    for table in await repository.fetch_tables_by_ids(neighbor_ids - current_ids):
        table["score"] = 0.99
        tables.insert(1, table)

    deduplicated: dict[int, dict[str, Any]] = {}
    for table in tables:
        table_id = int(table["id"])
        current = deduplicated.get(table_id)
        if current is None or float(table.get("score") or 0) > float(
            current.get("score") or 0
        ):
            deduplicated[table_id] = table
    tables = sorted(
        deduplicated.values(),
        key=lambda item: float(item.get("score") or 0),
        reverse=True,
    )[:effective_top_k]

    table_ids = [int(table["id"]) for table in tables]
    columns = (
        await repository.search_columns(
            embedding,
            table_ids=table_ids,
            per_table_limit=column_top_m or runtime.decision.column_top_m,
        )
        if include_matched_columns
        else {}
    )
    mapping_table_ids = {
        int(mapping["table_id"])
        for mapping in mappings
        if mapping.get("table_id") is not None
    }
    candidates = [
        _candidate(
            table,
            columns.get(int(table["id"]), []),
            source="name_rule"
            if int(table["id"]) in mapping_table_ids
            else "vector",
        )
        for table in tables
    ]

    fk_bridges_raw = await repository.fk_bridges(table_ids)
    bridges = [
        JoinBridge(
            **{
                "from": (
                    f"{row['from_schema']}.{row['from_table']}."
                    f"{row['from_column']}"
                ),
                "to": (
                    f"{row['to_schema']}.{row['to_table']}."
                    f"{row['to_column']}"
                ),
                "via": "fk",
                "confidence": runtime.decision.verified_join_confidence,
            }
        )
        for row in fk_bridges_raw
    ]
    convention_raw = await repository.convention_bridges(table_ids)
    existing_bridge_keys = {(bridge.from_, bridge.to) for bridge in bridges}
    for row in convention_raw:
        from_value = (
            f"{row['from_schema']}.{row['from_table']}.{row['from_column']}"
        )
        to_value = f"{row['to_schema']}.{row['to_table']}.{row['to_column']}"
        if (from_value, to_value) in existing_bridge_keys:
            continue
        bridges.append(
            JoinBridge(
                **{
                    "from": from_value,
                    "to": to_value,
                    "via": "convention",
                    "confidence": runtime.decision.convention_join_confidence,
                }
            )
        )
    join_groups = []
    if bridges:
        join_groups.append(
            JoinGroup(
                members=[
                    TableKey(
                        db=candidate.db,
                        schema_name=candidate.schema_name,
                        table_name=candidate.table_name,
                    )
                    for candidate in candidates
                ],
                recommended_strategy="simple_join",
                bridges=bridges,
                group_score=max(bridge.confidence for bridge in bridges),
                rationale="물리 FK 또는 승인된 논리 join 힌트와 동일 식별자 컬럼 연결 후보",
            )
        )

    entities = _resolved_entities(mappings) if auto_resolve_entities else []
    return DecisionResponse(
        target="source" if candidates else "none",
        secondary_targets=[],
        confidence=float(candidates[0].score) if candidates else 0.0,
        candidates=candidates,
        join_groups=join_groups,
        threshold_used={
            "minimum_similarity": runtime.decision.minimum_similarity,
            "table_top_k": effective_top_k,
            "table_limit": table_limit,
            "column_top_m": column_top_m or runtime.decision.column_top_m,
        },
        resolved_entities=entities,
        suggested_probes=[],
        resolution_status="complete" if entities else "partial",
        execution_context=ExecutionContext(
            backend=runtime.execution.backend,
            dialect=runtime.execution.dialect,
            integration=runtime.execution.integration,
            catalog=runtime.execution.catalog,
            schema_name=runtime.execution.schema,
            qualification_pattern=runtime.execution.qualification_pattern,
            identifier_quote=runtime.execution.identifier_quote,
            require_quoted_uppercase_identifiers=(
                runtime.execution.require_quoted_uppercase_identifiers
            ),
        ),
    )
