from __future__ import annotations

from collections import defaultdict
import json
from typing import Any

from ...schemas import (
    DecisionCandidate,
    MatchedColumn,
    ResolvedEntity,
    ResolvedValue,
)
from .. import subject_area as subject_area_service
from .default_date import default_date_column
from .table_type import list_table_type


def _same_source(row: dict[str, Any], source_instance_id: str) -> bool:
    return str(row.get("source_instance_id") or "") == source_instance_id


def _provisional_source_instance_id(
    store_rows: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
) -> str:
    """Prefer Store mapping/catalog hits. Never prefer a vector winner."""

    for mapping in mappings:
        source_id = str(mapping.get("source_instance_id") or "").strip()
        if source_id:
            return source_id
    for row in store_rows:
        source_id = str(row.get("source_instance_id") or "").strip()
        if source_id:
            return source_id
    return ""


def _target_class(subject_area: str) -> str:
    if subject_area == "agg":
        return "analytic"
    if subject_area in {"raw", "master", "code"}:
        return "source"
    if subject_area in {"hist", "link"}:
        return "collect"
    return "unknown"


def _serving_logical_name(table: dict[str, Any]) -> str | None:
    value = str(table.get("logical_name") or "").strip()
    if not value or value == "「미정」":
        return None
    return value


def _resolve_subject_area(table: dict[str, Any]) -> str:
    """Prefer platform-published subject_area; fall back to local YAML rules."""
    for key in ("subject_area_override", "subject_area"):
        value = str(table.get(key) or "").strip().casefold()
        if value:
            return value
    return subject_area_service.classify(
        str(table.get("schema_name") or ""),
        str(table.get("original_name") or table.get("name") or ""),
    )


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _value_examples(metadata: dict[str, Any]) -> list[str]:
    sample_values = metadata.get("sample_values")
    if not isinstance(sample_values, list):
        return []
    examples: list[str] = []
    for item in sample_values:
        value = item.get("value") if isinstance(item, dict) else item
        if value is None or isinstance(value, (dict, list)):
            continue
        examples.append(str(value))
        if len(examples) == 5:
            break
    return examples


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _merge_column_hits(
    question_hits: dict[int, list[dict[str, Any]]],
    metric_hits: dict[int, list[dict[str, Any]]],
) -> dict[int, list[dict[str, Any]]]:
    merged: dict[int, list[dict[str, Any]]] = {}
    for table_id in question_hits.keys() | metric_hits.keys():
        by_column_id: dict[int, dict[str, Any]] = {}
        for column in [
            *question_hits.get(table_id, []),
            *metric_hits.get(table_id, []),
        ]:
            column_id = int(column["id"])
            current = by_column_id.get(column_id)
            if current is None or float(column.get("score") or 0.0) > float(
                current.get("score") or 0.0
            ):
                by_column_id[column_id] = column
        merged[table_id] = sorted(
            by_column_id.values(),
            key=lambda item: float(item.get("score") or 0.0),
            reverse=True,
        )
    return merged


def _candidate(
    table: dict[str, Any],
    columns: list[dict[str, Any]],
    *,
    source: str,
) -> DecisionCandidate:
    subject_area = _resolve_subject_area(table)
    matched: list[MatchedColumn] = []
    for column in columns:
        metadata = _metadata_dict(column.get("metadata"))
        constraints = []
        if column.get("is_primary_key"):
            constraints.append("PK")
        if column.get("is_foreign_key"):
            constraints.append("FK")
        matched.append(
            MatchedColumn(
                column_name=str(column["name"]),
                score=float(column.get("score") or 0.0),
                constraints=constraints,
                column_name_kr=(column.get("description") or None),
                data_type=(column.get("dtype") or None),
                description=(
                    column.get("analyzed_description")
                    or column.get("description")
                    or None
                ),
                value_examples=_value_examples(metadata),
                format_pattern=_optional_string(metadata.get("format_pattern")),
                unit=_optional_string(metadata.get("unit")),
                facility_code=_optional_string(
                    metadata.get("facility_code") or metadata.get("facility_scope")
                ),
                system_code=_optional_string(metadata.get("system_code")),
                pk_ordinal=_optional_int(metadata.get("pk_ordinal")),
            )
        )
    return DecisionCandidate(
        db=str(table.get("source_name") or table.get("db") or ""),
        schema_name=str(table.get("schema_name") or ""),
        table_name=str(table.get("original_name") or table.get("name") or ""),
        score=float(table.get("score") or 0.0),
        source=source,
        target_class=_target_class(subject_area),
        subject_area=subject_area,
        matched_columns=matched,
        table_comment=(table.get("description") or None),
        description=(
            table.get("analyzed_description")
            or table.get("description")
            or None
        ),
        logical_name=_serving_logical_name(table),
        table_type=list_table_type(subject_area),
        default_date_column=default_date_column(columns),
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
        column_name = str(rows[0].get("column_name") or "")
        metadata = _metadata_dict(rows[0].get("metadata"))
        label_column = _optional_string(metadata.get("label_column"))
        entities.append(
            ResolvedEntity(
                mention=mention,
                entity_type="code",
                db=db or None,
                schema_name=schema_name or None,
                table=table_name,
                name_column=label_column or column_name,
                code_column=column_name,
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
