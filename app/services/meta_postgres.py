from __future__ import annotations

from typing import Any

from ..schemas import (
    BatchItem,
    CodeLookup,
    ColumnMeta,
    DataSourceInfo,
    MetaTableResponse,
    RefMeta,
    SubjectArea,
    TableInfo,
)
from .decision_postgres.default_date import default_date_column
from .decision_postgres.helpers import (
    _column_has_code,
    _column_name_kr,
    _format_pattern,
    _metadata_dict,
    _optional_string,
    _pk_ordinal,
    _resolve_subject_area,
    _serving_data_type,
    _serving_logical_name,
    _value_examples,
)
from .decision_postgres.table_type import list_table_type
from .metadata_repository import PostgresMetadataRepository

_SUBJECT_AREAS = frozenset(
    {"agg", "raw", "code", "hist", "master", "link", "unknown"}
)
_SERVING_TABLE_KEYS = ("subject_area", "subject_area_override", "logical_name")


def _flatten_table(table: dict[str, Any]) -> dict[str, Any]:
    """Copy Store metadata keys that /data_decision already flattens in SQL."""

    metadata = _metadata_dict(table.get("metadata"))
    row = dict(table)
    for key in _SERVING_TABLE_KEYS:
        if str(row.get(key) or "").strip():
            continue
        value = metadata.get(key)
        if value is not None and str(value).strip():
            row[key] = value
    return row


def _as_subject_area(table: dict[str, Any]) -> SubjectArea:
    value = _resolve_subject_area(table)
    if value in _SUBJECT_AREAS:
        return value  # type: ignore[return-value]
    return "unknown"


def _analyzed_or_original(*values: Any) -> str | None:
    for value in values:
        text = _optional_string(value)
        if text:
            return text
    return None


def _constraints(column: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    constraints: list[str] = []
    if column.get("is_primary_key"):
        constraints.append("PK")
    if column.get("is_foreign_key"):
        constraints.append("FK")
    if bool(metadata.get("is_unique")):
        constraints.append("UNIQUE")
    return constraints


def _code_lookup(
    column_name: str,
    refs: list[dict[str, Any]] | None,
) -> CodeLookup | None:
    needle = str(column_name or "").strip().casefold()
    if not needle:
        return None
    for ref in refs or []:
        if str(ref.get("column_name") or "").strip().casefold() != needle:
            continue
        schema_name = _optional_string(ref.get("ref_schema_name"))
        table_name = _optional_string(ref.get("ref_table_name"))
        ref_column = _optional_string(ref.get("ref_column_name"))
        if not schema_name or not table_name or not ref_column:
            continue
        return CodeLookup(
            ref_schema_name=schema_name,
            ref_table_name=table_name,
            ref_column_name=ref_column,
            via="fk",
        )
    return None


def _normalize_column(column: dict[str, Any]) -> dict[str, Any]:
    row = dict(column)
    row["metadata"] = _metadata_dict(column.get("metadata"))
    return row


def _column_meta(
    column: dict[str, Any],
    *,
    refs: list[dict[str, Any]] | None = None,
    code_columns: set[str] | None = None,
) -> ColumnMeta:
    row = _normalize_column(column)
    metadata = row["metadata"]
    examples = _value_examples(metadata)
    original = _optional_string(row.get("description"))
    return ColumnMeta(
        column_name=str(row["name"]),
        column_name_kr=_column_name_kr(row, metadata),
        data_type=_serving_data_type(row, metadata),
        column_comment=original,
        description=_analyzed_or_original(
            row.get("analyzed_description"),
            row.get("description"),
        ),
        constraints=_constraints(row, metadata),
        is_null=bool(row.get("nullable")),
        column_is_active="Y",
        format_pattern=_format_pattern(metadata, examples),
        unit=_optional_string(metadata.get("unit")),
        facility_code=_optional_string(
            metadata.get("facility_code") or metadata.get("facility_scope")
        ),
        system_code=_optional_string(metadata.get("system_code")),
        pk_ordinal=_pk_ordinal(row, metadata),
        has_code=_column_has_code(str(row["name"]), code_columns),
        code_lookup=_code_lookup(str(row["name"]), refs),
        term_mapping=None,
        value_examples=examples,
    )


def _table_info(
    table: dict[str, Any],
    columns: list[dict[str, Any]],
) -> TableInfo:
    row = _flatten_table(table)
    normalized = [_normalize_column(column) for column in columns]
    subject_area = _as_subject_area(row)
    original = _optional_string(row.get("description"))
    return TableInfo(
        db=str(row.get("db") or ""),
        schema_name=str(row.get("schema_name") or ""),
        table_name=str(row.get("original_name") or row.get("table_name") or ""),
        table_name_kr=_serving_logical_name(row),
        table_comment=original,
        description=_analyzed_or_original(
            row.get("analyzed_description"),
            row.get("description"),
        ),
        subject_area=subject_area,
        table_type=list_table_type(subject_area),
        default_date_column=default_date_column(normalized),
        table_is_active="Y",
        pk_columns=[
            str(column["name"])
            for column in normalized
            if column.get("is_primary_key")
        ],
        datasource=DataSourceInfo(
            id=str(row.get("profile_id") or row.get("db") or ""),
            dialect=(row.get("engine") or None),
            domain=None,
            owner=None,
        ),
        lineage_brief={"upstream": [], "downstream": []},
        ontology_anchors=[],
    )


def _batch_item(row: dict[str, Any]) -> BatchItem:
    table = _flatten_table(row)
    subject_area = _as_subject_area(table)
    original = _optional_string(table.get("description"))
    return BatchItem(
        db=str(table.get("db") or "") or None,
        schema_name=str(table.get("schema_name") or ""),
        table_name=str(table.get("table_name") or table.get("original_name") or ""),
        table_name_kr=_serving_logical_name(table),
        table_comment=original,
        description=_analyzed_or_original(
            table.get("analyzed_description"),
            table.get("description"),
        ),
        subject_area=subject_area,
    )


async def _code_columns_for_table(
    repository: PostgresMetadataRepository,
    table: dict[str, Any],
) -> set[str]:
    table_id = table.get("id")
    if table_id is None:
        return set()
    found = await repository.find_value_mapping_code_columns([int(table_id)])
    return found.get(int(table_id), set())


async def list_batch(
    repository: PostgresMetadataRepository,
    *,
    batch_date: str | None,
) -> list[BatchItem]:
    _ = batch_date
    rows = await repository.list_tables()
    return [_batch_item(row) for row in rows]


async def get_table(
    repository: PostgresMetadataRepository,
    *,
    db: str | None,
    schema_name: str,
    table_name: str,
) -> MetaTableResponse | None:
    detail = await repository.fetch_table_detail(
        db=db,
        schema_name=schema_name,
        table_name=table_name,
    )
    if detail is None:
        return None
    table = detail["table"]
    columns = detail["columns"]
    refs = detail["refs"]
    code_columns = await _code_columns_for_table(repository, table)
    return MetaTableResponse(
        table_info=_table_info(table, columns),
        columns=[
            _column_meta(column, refs=refs, code_columns=code_columns)
            for column in columns
        ],
        fk=[RefMeta(**row) for row in refs],
    )


async def get_column(
    repository: PostgresMetadataRepository,
    *,
    db: str | None,
    schema_name: str,
    table_name: str,
    column_name: str,
) -> ColumnMeta | None:
    detail = await repository.fetch_table_detail(
        db=db,
        schema_name=schema_name,
        table_name=table_name,
    )
    if detail is None:
        return None
    needle = column_name.lower()
    column = next(
        (
            item
            for item in detail["columns"]
            if str(item["name"]).lower() == needle
        ),
        None,
    )
    if column is None:
        return None
    code_columns = await _code_columns_for_table(repository, detail["table"])
    return _column_meta(
        column,
        refs=detail["refs"],
        code_columns=code_columns,
    )


async def get_refs(
    repository: PostgresMetadataRepository,
    *,
    db: str | None,
    schema_name: str,
    table_name: str,
) -> list[RefMeta]:
    rows = await repository.fetch_refs(
        db=db,
        schema_name=schema_name,
        table_name=table_name,
    )
    return [RefMeta(**row) for row in rows]
