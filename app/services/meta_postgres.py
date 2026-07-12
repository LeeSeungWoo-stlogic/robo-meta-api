from __future__ import annotations

import json

from ..schemas import (
    BatchItem,
    ColumnMeta,
    DataSourceInfo,
    MetaTableResponse,
    RefMeta,
    TableInfo,
)
from .metadata_repository import PostgresMetadataRepository


def _constraints(column: dict) -> list[str]:
    constraints: list[str] = []
    if column.get("is_primary_key"):
        constraints.append("PK")
    if column.get("is_foreign_key"):
        constraints.append("FK")
    metadata = column.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    if bool(metadata.get("is_unique")):
        constraints.append("UNIQUE")
    return constraints


def _column_meta(column: dict) -> ColumnMeta:
    return ColumnMeta(
        column_name=str(column["name"]),
        column_name_kr=(column.get("description") or None),
        data_type=(column.get("dtype") or None),
        column_comment=(
            column.get("analyzed_description")
            or column.get("description")
            or None
        ),
        constraints=_constraints(column),
        is_null=bool(column.get("nullable")),
        column_is_active="Y",
        code_lookup=None,
        term_mapping=None,
        value_examples=[],
    )


async def list_batch(
    repository: PostgresMetadataRepository,
    *,
    batch_date: str | None,
) -> list[BatchItem]:
    _ = batch_date
    rows = await repository.list_tables()
    return [BatchItem(**row) for row in rows]


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
    return MetaTableResponse(
        table_info=TableInfo(
            db=str(table["db"]),
            schema_name=str(table["schema_name"]),
            table_name=str(table["original_name"]),
            table_name_kr=(table.get("description") or None),
            table_comment=(
                table.get("analyzed_description")
                or table.get("description")
                or None
            ),
            subject_area="unknown",
            table_is_active="Y",
            pk_columns=[
                str(column["name"])
                for column in columns
                if column.get("is_primary_key")
            ],
            datasource=DataSourceInfo(
                id=str(table.get("profile_id") or table["db"]),
                dialect=(table.get("engine") or None),
                domain=None,
                owner=None,
            ),
            lineage_brief={"upstream": [], "downstream": []},
            ontology_anchors=[],
        ),
        columns=[_column_meta(column) for column in columns],
        fk=[RefMeta(**row) for row in detail["refs"]],
    )


async def get_column(
    repository: PostgresMetadataRepository,
    *,
    db: str | None,
    schema_name: str,
    table_name: str,
    column_name: str,
) -> ColumnMeta | None:
    column = await repository.fetch_column(
        db=db,
        schema_name=schema_name,
        table_name=table_name,
        column_name=column_name,
    )
    return _column_meta(column) if column is not None else None


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
