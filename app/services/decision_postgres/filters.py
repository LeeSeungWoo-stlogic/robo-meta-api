from __future__ import annotations

import re
from typing import Any

from ...schemas import FilterRequirement, PlannedFilter, QueryAnalysis
from ..metadata_repository import PostgresMetadataRepository


def _compact_text(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def _mapping_matches_value_text(mapping: dict[str, Any], value_text: str | None) -> bool:
    if not value_text:
        return False
    natural = _compact_text(str(mapping.get("natural_value") or ""))
    target = _compact_text(value_text)
    return bool(natural) and natural in target


def _plan_column_from_fqn(column_fqn: str) -> str:
    parts = column_fqn.split(".")
    # Store seed uses db.schema.table.column; plan uses schema.table.column.
    if len(parts) >= 3:
        return ".".join(parts[-3:])
    return column_fqn


def _with_metric_filter_requirements(
    analysis: QueryAnalysis,
    mappings: list[dict[str, Any]] | None = None,
) -> list[FilterRequirement]:
    """Promote measurement.metric into a filter slot when Store VM can resolve it.

    Generic: no table/column names. Uses analyzer metric text + verified mappings.
    """
    requirements = list(analysis.filter_requirements)
    metric = str(analysis.measurement.metric or "").strip()
    if not metric:
        return requirements
    if mappings is not None and not any(
        _mapping_matches_value_text(mapping, metric) for mapping in mappings
    ):
        return requirements
    compact_metric = _compact_text(metric)
    for requirement in requirements:
        if requirement.value_text and _compact_text(requirement.value_text) == compact_metric:
            return requirements
        if compact_metric and compact_metric in _compact_text(requirement.meaning):
            return requirements
    requirements.append(
        FilterRequirement(
            meaning=f"측정항목:{metric}",
            required=True,
            operator_hint="EQ",
            value_text=metric,
        )
    )
    return requirements


def _filter_column_score(
    requirement: FilterRequirement,
    column: dict[str, Any],
) -> float:
    base = float(column.get("score") or 0.0)
    meaning = requirement.meaning.lower()
    name = str(column.get("name") or "").upper()
    descriptions = " ".join(
        str(column.get(key) or "").lower()
        for key in ("description", "analyzed_description")
    )
    boosts = (
        (("오류", "에러"), ("ERR", "ERROR")),
        (("상태",), ("STATUS", "STATE")),
        (("시간", "시각", "기간"), ("TIME", "DATE", "DT", "TM")),
        (("이름", "명칭"), ("NAME", "NM", "DESC")),
        (("코드",), ("CODE", "CD")),
        (("값", "측정"), ("VALUE", "VAL")),
    )
    lexical = 0.0
    for korean_terms, physical_terms in boosts:
        if any(term in meaning for term in korean_terms) and any(
            term in name for term in physical_terms
        ):
            lexical = max(lexical, 0.45)
    tokens = {
        token
        for token in re.findall(r"[가-힣]{2,}", meaning)
        if token not in {"있는", "항목", "조회"}
    }
    if tokens and any(token in descriptions for token in tokens):
        lexical = max(lexical, 0.25)
    return base + lexical


async def _resolve_filters(
    repository: PostgresMetadataRepository,
    *,
    requirements: list[FilterRequirement],
    embeddings: dict[str, list[float]],
    table_ids: list[int],
    tables_by_id: dict[int, dict[str, Any]],
    mappings: list[dict[str, Any]],
    minimum_similarity: float,
) -> tuple[list[PlannedFilter], list[str]]:
    planned: list[PlannedFilter] = []
    unresolved: list[str] = []
    for index, requirement in enumerate(requirements):
        embedding = embeddings.get(f"filter:{index}")
        best: dict[str, Any] | None = None
        if embedding is not None and table_ids:
            grouped = await repository.search_columns(
                embedding,
                table_ids=table_ids,
                per_table_limit=5,
            )
            options = [
                item
                for values in grouped.values()
                for item in values
            ]
            if options:
                best = max(
                    options,
                    key=lambda item: _filter_column_score(
                        requirement,
                        item,
                    ),
                )

        mapped = next(
            (
                mapping
                for mapping in mappings
                if _mapping_matches_value_text(mapping, requirement.value_text)
            ),
            None,
        )
        # Prefer verified code mapping column over weak semantic column hits.
        if mapped is not None and mapped.get("column_fqn"):
            column = _plan_column_from_fqn(str(mapped["column_fqn"]))
            value = str(mapped.get("code_value") or requirement.value_text)
            planned.append(
                PlannedFilter(
                    meaning=requirement.meaning,
                    column=column,
                    operator="EQ",
                    value=value,
                    resolution_status="resolved",
                    confidence=1.0,
                )
            )
            continue

        score = min(1.0, (
            _filter_column_score(requirement, best)
            if best
            else 0.0
        ))
        is_resolved = bool(best and score >= minimum_similarity)
        table = (
            tables_by_id.get(int(best["table_id"]))
            if best and best.get("table_id") is not None
            else None
        )
        column = (
            f"{table.get('schema_name')}.{table.get('original_name') or table.get('name')}."
            f"{best.get('name')}"
            if table and best
            else None
        )
        value = requirement.value_text
        planned.append(
            PlannedFilter(
                meaning=requirement.meaning,
                column=column or None,
                operator=requirement.operator_hint,
                value=value,
                resolution_status="resolved" if is_resolved else "unresolved",
                confidence=score,
            )
        )
        if requirement.required and not is_resolved:
            unresolved.append(f"필수 필터 컬럼 미해결: {requirement.meaning}")
    return planned, unresolved


def _filter_endpoint_key(schema: str, table: str, column: str) -> tuple[str, str, str]:
    return (
        str(schema or "").casefold(),
        str(table or "").casefold(),
        str(column or "").casefold(),
    )


def _parse_plan_column(column: str | None) -> tuple[str, str, str] | None:
    if not column:
        return None
    parts = column.split(".")
    if len(parts) < 3:
        return None
    return _filter_endpoint_key(parts[-3], parts[-2], parts[-1])


def _propagate_filters_along_fk(
    planned: list[PlannedFilter],
    edge_rows: list[dict[str, Any]],
    *,
    anchor_table_ids: set[int],
    tables_by_id: dict[int, dict[str, Any]],
) -> list[PlannedFilter]:
    """Copy resolved EQ filters across one approved FK hop onto plan/bridge tables.

    Uses only Store `t2s_fk_constraints` edges. Does not invent table/column names.
    """
    if not planned or not edge_rows or not anchor_table_ids:
        return planned

    existing = {
        (filter_.column, filter_.operator, filter_.value)
        for filter_ in planned
        if filter_.column
    }
    # Undirected adjacency: endpoint -> [(other_table_id, other_schema, other_table, other_col)]
    adjacency: dict[tuple[str, str, str], list[tuple[int, str, str, str]]] = {}
    for edge in edge_rows:
        from_key = _filter_endpoint_key(
            str(edge.get("from_schema") or ""),
            str(edge.get("from_table") or ""),
            str(edge.get("from_column") or ""),
        )
        to_key = _filter_endpoint_key(
            str(edge.get("to_schema") or ""),
            str(edge.get("to_table") or ""),
            str(edge.get("to_column") or ""),
        )
        adjacency.setdefault(from_key, []).append(
            (
                int(edge["to_table_id"]),
                str(edge.get("to_schema") or ""),
                str(edge.get("to_table") or ""),
                str(edge.get("to_column") or ""),
            )
        )
        adjacency.setdefault(to_key, []).append(
            (
                int(edge["from_table_id"]),
                str(edge.get("from_schema") or ""),
                str(edge.get("from_table") or ""),
                str(edge.get("from_column") or ""),
            )
        )

    extra: list[PlannedFilter] = []
    for filter_ in planned:
        if filter_.resolution_status != "resolved":
            continue
        if filter_.operator != "EQ" or filter_.value is None or not filter_.column:
            continue
        endpoint = _parse_plan_column(filter_.column)
        if endpoint is None:
            continue
        for table_id, schema, table, column in adjacency.get(endpoint, []):
            if table_id not in anchor_table_ids:
                continue
            # Prefer published table identity from tables_by_id when present.
            table_row = tables_by_id.get(table_id)
            if table_row is not None:
                schema = str(table_row.get("schema_name") or schema)
                table = str(
                    table_row.get("original_name") or table_row.get("name") or table
                )
            target_column = f"{schema}.{table}.{column}"
            key = (target_column, "EQ", filter_.value)
            if key in existing:
                continue
            existing.add(key)
            extra.append(
                PlannedFilter(
                    meaning=f"{filter_.meaning}→FK",
                    column=target_column,
                    operator="EQ",
                    value=filter_.value,
                    resolution_status="resolved",
                    confidence=min(1.0, float(filter_.confidence or 0.0) * 0.95),
                )
            )
    if not extra:
        return planned
    return [*planned, *extra]
