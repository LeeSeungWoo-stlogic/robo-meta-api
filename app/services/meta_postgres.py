from __future__ import annotations

from typing import Any

from ..schemas import (
    BatchItem,
    CatalogColumn,
    CatalogColumnReference,
    CatalogResponse,
    CatalogSource,
    CatalogTable,
    CodeLookup,
    ColumnMeta,
    DataSourceInfo,
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
    _optional_int,
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


def _norm_key(value: str | None) -> str:
    return str(value or "").strip().casefold()


def catalog_has_tables(catalog: CatalogResponse) -> bool:
    return any(source.tables for source in catalog.sources)


def catalog_has_columns(catalog: CatalogResponse) -> bool:
    return any(
        column
        for source in catalog.sources
        for table in source.tables
        for column in table.columns
    )


def slice_serving_catalog(
    catalog: CatalogResponse,
    *,
    source_name: str | None = None,
    engine: str | None = None,
    schema_name: str,
    table_name: str,
    column_name: str | None = None,
    refs_only: bool = False,
) -> CatalogResponse:
    source_key = _norm_key(source_name)
    engine_key = _norm_key(engine)
    schema_key = _norm_key(schema_name)
    table_key = _norm_key(table_name)
    column_key = _norm_key(column_name)
    sources: list[CatalogSource] = []
    for source in catalog.sources:
        if source_key and _norm_key(source.source_name) != source_key:
            continue
        if engine_key and _norm_key(source.engine) != engine_key:
            continue
        tables: list[CatalogTable] = []
        if schema_key and _norm_key(source.source_schema) != schema_key:
            continue
        for table in source.tables:
            if _norm_key(table.table_name) != table_key:
                continue
            columns = list(table.columns)
            if column_key:
                columns = [
                    column
                    for column in columns
                    if _norm_key(column.column_name) == column_key
                ]
            if refs_only:
                columns = [
                    column
                    for column in columns
                    if column.references is not None or column.referenced_by
                ]
            if column_key and not columns:
                continue
            tables.append(table.model_copy(update={"columns": columns}))
        if tables:
            sources.append(source.model_copy(update={"tables": tables}))
    return CatalogResponse(
        serving_status=catalog.serving_status,
        sources=sources,
    )


def batch_items_from_catalog(catalog: CatalogResponse) -> list[BatchItem]:
    items: list[BatchItem] = []
    for source in catalog.sources:
        for table in source.tables:
            items.append(
                BatchItem(
                    source_name=source.source_name or None,
                    engine=source.engine or None,
                    schema_name=source.source_schema or "",
                    table_name=table.table_name,
                    table_name_kr=table.logical_name,
                    table_comment=table.comment,
                    description=table.description,
                    subject_area=table.subject_area,
                )
            )
    return items


def refs_from_catalog(catalog: CatalogResponse) -> list[RefMeta]:
    refs: list[RefMeta] = []
    for source in catalog.sources:
        for table in source.tables:
            for column in table.columns:
                ref = column.references
                if ref is None:
                    continue
                refs.append(
                    RefMeta(
                        column_name=column.column_name,
                        position=ref.position,
                        ref_schema_name=ref.schema_name,
                        ref_table_name=ref.table_name,
                        ref_column_name=ref.column_name,
                    )
                )
    return refs


async def list_batch(
    repository: PostgresMetadataRepository,
    *,
    batch_date: str | None,
) -> list[BatchItem]:
    _ = batch_date
    catalog = await get_serving_catalog(repository)
    return batch_items_from_catalog(catalog)


async def get_table(
    repository: PostgresMetadataRepository,
    *,
    source_name: str | None = None,
    engine: str | None = None,
    db: str | None = None,
    schema_name: str,
    table_name: str,
) -> CatalogResponse | None:
    _ = db
    catalog = slice_serving_catalog(
        await get_serving_catalog(repository),
        source_name=source_name,
        engine=engine,
        schema_name=schema_name,
        table_name=table_name,
    )
    if not catalog_has_tables(catalog):
        return None
    return catalog


async def get_column(
    repository: PostgresMetadataRepository,
    *,
    source_name: str | None = None,
    engine: str | None = None,
    db: str | None = None,
    schema_name: str,
    table_name: str,
    column_name: str,
) -> CatalogResponse | None:
    _ = db
    catalog = slice_serving_catalog(
        await get_serving_catalog(repository),
        source_name=source_name,
        engine=engine,
        schema_name=schema_name,
        table_name=table_name,
        column_name=column_name,
    )
    if not catalog_has_columns(catalog):
        return None
    return catalog


async def get_refs(
    repository: PostgresMetadataRepository,
    *,
    source_name: str | None = None,
    engine: str | None = None,
    db: str | None = None,
    schema_name: str,
    table_name: str,
) -> list[RefMeta]:
    _ = db
    catalog = slice_serving_catalog(
        await get_serving_catalog(repository),
        source_name=source_name,
        engine=engine,
        schema_name=schema_name,
        table_name=table_name,
        refs_only=True,
    )
    return refs_from_catalog(catalog)


def _catalog_data_type(column: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    raw = _serving_data_type(column, metadata)
    if not raw:
        return None
    prefix = "character varying"
    if raw.casefold().startswith(prefix):
        return "varchar" + raw[len(prefix) :]
    return raw


def _catalog_reference(
    row: dict[str, Any],
    *,
    schema_key: str,
    table_key: str,
    column_key: str,
    constraint_key: str,
    position_key: str,
) -> CatalogColumnReference | None:
    schema_name = _optional_string(row.get(schema_key))
    table_name = _optional_string(row.get(table_key))
    column_name = _optional_string(row.get(column_key))
    if not schema_name or not table_name or not column_name:
        return None
    return CatalogColumnReference(
        schema_name=schema_name,
        table_name=table_name,
        column_name=column_name,
        constraint_name=_optional_string(row.get(constraint_key)),
        position=_optional_int(row.get(position_key)) or 1,
    )


def _append_unique_reference(
    items: list[CatalogColumnReference] | None,
    extra: CatalogColumnReference | None,
) -> list[CatalogColumnReference] | None:
    if extra is None:
        return items
    current = list(items or [])
    if extra in current:
        return current or None
    current.append(extra)
    return current


def _catalog_column(row: dict[str, Any]) -> CatalogColumn:
    metadata = _metadata_dict(row.get("metadata"))
    column = {"dtype": row.get("dtype"), "metadata": metadata}
    comment = _optional_string(row.get("column_comment"))
    referenced_by = _append_unique_reference(
        None,
        _catalog_reference(
            row,
            schema_key="inbound_schema_name",
            table_key="inbound_table_name",
            column_key="inbound_column_name",
            constraint_key="inbound_constraint_name",
            position_key="inbound_position",
        ),
    )
    return CatalogColumn(
        column_name=str(row.get("column_name") or ""),
        data_type=_catalog_data_type(column, metadata),
        nullable=bool(row.get("nullable")),
        primary_key=bool(row.get("is_primary_key")),
        comment=comment,
        description=_analyzed_or_original(
            row.get("column_description"),
            row.get("column_comment"),
        ),
        references=_catalog_reference(
            row,
            schema_key="ref_schema_name",
            table_key="ref_table_name",
            column_key="ref_column_name",
            constraint_key="ref_constraint_name",
            position_key="ref_position",
        ),
        referenced_by=referenced_by,
    )


def _catalog_table_from_row(row: dict[str, Any]) -> CatalogTable:
    table = _flatten_table(
        {
            "schema_name": row.get("schema_name"),
            "original_name": row.get("table_name"),
            "description": row.get("table_comment"),
            "analyzed_description": row.get("table_description"),
            "metadata": row.get("table_metadata"),
        }
    )
    original = _optional_string(table.get("description"))
    return CatalogTable(
        table_name=str(row.get("table_name") or ""),
        logical_name=_serving_logical_name(table),
        comment=original,
        description=_analyzed_or_original(
            table.get("analyzed_description"),
            table.get("description"),
        ),
        subject_area=_as_subject_area(table),
    )


def assemble_serving_catalog(
    rows: list[dict[str, Any]],
    *,
    serving_active: bool,
) -> CatalogResponse:
    sources: dict[tuple[str, str, str, str], CatalogSource] = {}
    tables: dict[tuple[str, str, str, str, str, str], CatalogTable] = {}
    columns: dict[tuple[str, str, str, str, str, str, str], CatalogColumn] = {}
    for row in rows:
        source_key = (
            str(row.get("source_name") or ""),
            str(row.get("engine") or ""),
            str(row.get("source_schema") or ""),
            str(row.get("registered_at") or ""),
        )
        if source_key not in sources:
            sources[source_key] = CatalogSource(
                source_name=source_key[0],
                engine=source_key[1],
                source_schema=source_key[2] or None,
                registered_at=source_key[3],
                tables=[],
            )
        table_key = source_key + (
            str(row.get("schema_name") or ""),
            str(row.get("table_name") or ""),
        )
        if not table_key[4] or not table_key[5]:
            continue
        if table_key not in tables:
            table = _catalog_table_from_row(row)
            tables[table_key] = table
            sources[source_key].tables.append(table)
        column_name = str(row.get("column_name") or "")
        if not column_name:
            continue
        column_key = table_key + (column_name,)
        if column_key not in columns:
            column = _catalog_column(row)
            columns[column_key] = column
            tables[table_key].columns.append(column)
            continue
        existing = columns[column_key]
        extra = _catalog_column(row)
        if existing.references is None and extra.references is not None:
            existing.references = extra.references
        existing.referenced_by = _append_unique_reference(
            existing.referenced_by,
            extra.referenced_by[0] if extra.referenced_by else None,
        )
    return CatalogResponse(
        serving_status="active" if serving_active else "inactive",
        sources=list(sources.values()),
    )


async def get_serving_catalog(
    repository: PostgresMetadataRepository,
) -> CatalogResponse:
    payload = await repository.fetch_serving_catalog()
    return assemble_serving_catalog(
        payload.get("rows") or [],
        serving_active=bool(payload.get("serving_active")),
    )
