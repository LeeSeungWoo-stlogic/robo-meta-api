from __future__ import annotations

from collections import defaultdict
import json
import re
from typing import Any

from ...schemas import (
    DecisionCandidate,
    MatchedColumn,
    ResolvedEntity,
    ResolvedValue,
)
from .. import subject_area as subject_area_service
from .aliases import peel_type_suffix
from .default_date import default_date_column
from .table_type import list_table_type
from ..metadata_repository._search import SearchMixin


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


_CHAR_LENGTH_TYPES = frozenset(
    {
        "char",
        "character",
        "nchar",
        "varchar",
        "varchar2",
        "nvarchar",
        "nvarchar2",
        "character varying",
    }
)
_NUMERIC_LENGTH_TYPES = frozenset({"number", "numeric", "decimal"})
_DATE_FORMAT_RES = (
    (re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$"), "YYYY-MM-DD HH24:MI:SS"),
    (re.compile(r"^\d{14}$"), "YYYYMMDDHH24MISS"),
    (re.compile(r"^\d{12}$"), "YYYYMMDDHHMI"),
    (re.compile(r"^\d{10}$"), "YYYYMMDDHH"),
    (re.compile(r"^\d{8}$"), "YYYYMMDD"),
    (re.compile(r"^\d{6}$"), "YYYYMM"),
)


def _column_name_kr(column: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    standardization = metadata.get("standardization")
    std = standardization if isinstance(standardization, dict) else {}
    properties = metadata.get("properties")
    props = properties if isinstance(properties, dict) else {}
    for value in (
        metadata.get("column_name_kr"),
        metadata.get("logical_name"),
        metadata.get("korean_name"),
        metadata.get("display_name"),
        column.get("logical_name"),
        std.get("proposed_logical_name"),
        props.get("korean_name"),
        props.get("logical_name"),
        props.get("column_name_kr"),
    ):
        text = _optional_string(value)
        if text and text != "「미정」":
            return text
    return None


def _pk_ordinal(column: dict[str, Any], metadata: dict[str, Any]) -> int | None:
    ordinal = _optional_int(metadata.get("pk_ordinal"))
    if ordinal is not None:
        return ordinal
    if column.get("is_primary_key"):
        return 1
    return None


def _serving_data_type(column: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    explicit = _optional_string(metadata.get("data_type_with_length"))
    if explicit:
        return explicit
    raw = _optional_string(column.get("dtype"))
    if not raw:
        return None
    if "(" in raw:
        return raw
    folded = raw.casefold()
    if folded in _CHAR_LENGTH_TYPES:
        length = _optional_int(
            metadata.get("character_maximum_length") or metadata.get("data_length")
        )
        if length is not None and length > 0:
            return f"{raw}({length})"
        return raw
    if folded in _NUMERIC_LENGTH_TYPES:
        precision = _optional_int(
            metadata.get("numeric_precision") or metadata.get("data_precision")
        )
        scale = _optional_int(
            metadata.get("numeric_scale") or metadata.get("data_scale")
        )
        if precision is not None and precision > 0:
            if scale is not None and scale >= 0:
                return f"{raw}({precision},{scale})"
            return f"{raw}({precision})"
    return raw


def _format_pattern(metadata: dict[str, Any], examples: list[str]) -> str | None:
    published = _optional_string(metadata.get("format_pattern"))
    if published:
        return published
    return _infer_format_pattern(examples)


def _infer_format_pattern(values: list[str]) -> str | None:
    texts = [str(item).strip() for item in values if str(item).strip()]
    if len(texts) < 2:
        return None
    for matcher, pattern in _DATE_FORMAT_RES:
        if all(matcher.match(text) for text in texts):
            return pattern
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


def _column_has_code(column_name: str, code_columns: set[str] | None) -> str:
    name = str(column_name or "").strip().casefold()
    if not name:
        return "N"
    licensed = {item.strip().casefold() for item in (code_columns or set()) if str(item).strip()}
    return "Y" if name in licensed else "N"


def _candidate(
    table: dict[str, Any],
    columns: list[dict[str, Any]],
    *,
    source: str,
    code_columns: set[str] | None = None,
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
        examples = _value_examples(metadata)
        matched.append(
            MatchedColumn(
                column_name=str(column["name"]),
                score=float(column.get("score") or 0.0),
                constraints=constraints,
                column_name_kr=_column_name_kr(column, metadata),
                data_type=_serving_data_type(column, metadata),
                description=(
                    column.get("analyzed_description")
                    or column.get("description")
                    or None
                ),
                value_examples=examples,
                format_pattern=_format_pattern(metadata, examples),
                unit=_optional_string(metadata.get("unit")),
                facility_code=_optional_string(
                    metadata.get("facility_code") or metadata.get("facility_scope")
                ),
                system_code=_optional_string(metadata.get("system_code")),
                pk_ordinal=_pk_ordinal(column, metadata),
                has_code=_column_has_code(str(column["name"]), code_columns),
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


def _compact_surface(text: Any) -> str:
    return SearchMixin._compact_natural_text(str(text or ""))


def glossary_surfaces(
    groups: list[dict[str, Any]] | None = None,
    routes: list[dict[str, Any]] | None = None,
) -> set[str]:
    """Compact term/word surfaces that licensed this decide() lookup."""

    surfaces: set[str] = set()
    for group in groups or []:
        if str(group.get("kind") or "term") not in {"", "term"}:
            continue
        members = list(group.get("members") or [])
        for item in (group.get("preferred_form"), *members):
            key = _compact_surface(item)
            if key:
                surfaces.add(key)
    for row in routes or []:
        for field in ("mention", "standard_term", "word_korean"):
            key = _compact_surface(row.get(field))
            if key:
                surfaces.add(key)
    return surfaces


def _matched_mention(rows: list[dict[str, Any]]) -> str | None:
    for row in rows:
        mention = _optional_string(row.get("matched_mention"))
        if mention:
            return mention
    return None


def _entity_source(
    rows: list[dict[str, Any]],
    surfaces: set[str] | None = None,
) -> str:
    licensed = surfaces or set()
    for row in rows:
        if str(row.get("match_type") or "") == "alias_prefix":
            return "glossary"
        mention = _compact_surface(row.get("matched_mention"))
        natural = _compact_surface(row.get("natural_value"))
        if mention and mention in licensed:
            return "glossary"
        if mention and natural and mention != natural:
            peeled = peel_type_suffix(str(row.get("matched_mention") or ""))
            if peeled is not None:
                return "glossary"
    return "value_examples"


def _resolved_entities(
    mappings: list[dict[str, Any]],
    glossary_surfaces: set[str] | None = None,
) -> list[ResolvedEntity]:
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
                matched_mention=_matched_mention(rows),
                values=[
                    ResolvedValue(
                        code=str(row.get("code_value") or ""),
                        label=_optional_string(row.get("natural_value")),
                        confidence=1.0,
                    )
                    for row in rows
                ],
                source=_entity_source(rows, glossary_surfaces),
            )
        )
    return entities
