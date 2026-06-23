"""/meta/* 4 endpoint 의 Cypher 조회 모듈.

R-2: PRD §2 다이어그램은 `:Database` 표기였으나 실 적재 라벨은 `:DataSource` + `:Schema`.
     본 모듈의 Cypher 는 실 라벨을 사용한다.
R-3: db 라벨 폴백 체인 — Schema.db || DataSource.engine || settings.meta_db_label.
v0.6 RC 풍부 필드(`lineage_brief`, `ontology_anchors`, `code_lookup`, `term_mapping`,
`value_examples`) 는 본 단계에서 미적재이므로 빈 값 폴백 (`null` / `[]`).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from neo4j import AsyncDriver

from ..config import settings
from .kair_graph_adapter import (
    REL_COLUMN_FK,
    REL_SCHEMA_DATASOURCE,
    REL_TABLE_COLUMN,
    REL_TABLE_SCHEMA,
)
from ..schemas import (
    BatchItem,
    ColumnConstraint,
    ColumnMeta,
    DataSourceInfo,
    LineageBrief,
    MetaTableResponse,
    RefMeta,
    TableInfo,
    YN,
)

DEFAULT_DB = lambda: settings.meta_db_label  # noqa: E731


def _yn(v: Optional[bool]) -> YN:
    return "Y" if v else "N"


def _ds_info(schema_props: Dict[str, Any], ds_props: Dict[str, Any]) -> Optional[DataSourceInfo]:
    if not ds_props:
        return None
    return DataSourceInfo(
        id=str(ds_props.get("name") or ds_props.get("id") or "unknown"),
        dialect=ds_props.get("engine"),
        domain=None,
        owner=None,
    )


# ---------------------------------------------------------------------------
# /meta/batch
# ---------------------------------------------------------------------------
async def list_batch(driver: AsyncDriver, *, batch_date: Optional[str]) -> List[BatchItem]:
    """전체 테이블의 (db, schema_name, table_name) 목록.

    batch_date 는 본 단계에서 무시 — 변경/추가 메타가 아직 적재되지 않았으므로 항상 전체.
    추후 `Table.updated_at` 적재 후 필터 추가 가능.
    """
    cypher = f"""
    MATCH (t:Table)-[:{REL_TABLE_SCHEMA}]-(s:Schema)
    OPTIONAL MATCH (ds:DataSource)-[:{REL_SCHEMA_DATASOURCE}]->(s)
    WHERE COALESCE(t.text_to_sql_db_exists, true) = true
    RETURN COALESCE(s.db, ds.engine, $default_db) AS db,
           s.name AS schema_name,
           t.name AS table_name
    ORDER BY db, schema_name, table_name
    """
    async with driver.session() as sess:
        result = await sess.run(cypher, default_db=DEFAULT_DB())
        rows = await result.data()
    return [
        BatchItem(
            db=str(r["db"]),
            schema_name=str(r["schema_name"]),
            table_name=str(r["table_name"]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# /meta/table
# ---------------------------------------------------------------------------
async def get_table(
    driver: AsyncDriver,
    *,
    db: Optional[str],
    schema_name: str,
    table_name: str,
) -> Optional[MetaTableResponse]:
    """단일 테이블의 정의 + 컬럼 + FK 모두 조립.

    db 가 주어지면 Schema.db 와 일치하는 행을 우선 선택, 없으면 schema+name 만으로 매칭.
    """
    cypher = f"""
    MATCH (t:Table)-[:{REL_TABLE_SCHEMA}]-(s:Schema)
    WHERE toLower(s.name) = toLower($schema_name)
      AND toLower(t.name) = toLower($table_name)
      AND COALESCE(t.text_to_sql_db_exists, true) = true
    OPTIONAL MATCH (ds:DataSource)-[:{REL_SCHEMA_DATASOURCE}]->(s)
    WITH t, s, ds,
         COALESCE(s.db, ds.engine, $default_db) AS db_label
    WHERE $db_filter IS NULL OR toLower(db_label) = toLower($db_filter)
    OPTIONAL MATCH (t)-[:{REL_TABLE_COLUMN}]->(c:Column)
    WITH t, s, ds, db_label, collect(DISTINCT c) AS cols
    OPTIONAL MATCH (t)-[:{REL_TABLE_COLUMN}]->(c1:Column)-[fk:{REL_COLUMN_FK}]->(c2:Column)<-[:{REL_TABLE_COLUMN}]-(t2:Table)
    OPTIONAL MATCH (t2)-[:{REL_TABLE_SCHEMA}]-(s2:Schema)
    WITH t, s, ds, db_label, cols,
         collect(DISTINCT CASE WHEN c1 IS NULL THEN NULL ELSE {{
             from_column: c1.name,
             ref_schema_name: s2.name,
             ref_table_name: t2.name,
             ref_column_name: c2.name
         }} END) AS fks_raw
    RETURN db_label AS db,
           s.name AS schema_name,
           s {{.*}} AS schema_props,
           ds {{.*}} AS ds_props,
           t {{.*}} AS table_props,
           [c IN cols WHERE c IS NOT NULL | c {{.*}}] AS columns,
           [fk IN fks_raw WHERE fk IS NOT NULL] AS fks
    LIMIT 1
    """
    async with driver.session() as sess:
        result = await sess.run(
            cypher,
            schema_name=schema_name,
            table_name=table_name,
            db_filter=db,
            default_db=DEFAULT_DB(),
        )
        row = await result.single()
    if row is None:
        return None
    tp = row["table_props"] or {}
    cols = row["columns"] or []
    fks = row["fks"] or []

    pk_columns = [str(c.get("name")) for c in cols if c.get("is_primary_key") is True]

    table_info = TableInfo(
        db=str(row["db"]),
        schema_name=str(row["schema_name"]),
        table_name=str(tp.get("name") or table_name),
        table_name_kr=None,
        table_comment=tp.get("description"),
        subject_area="unknown",
        table_is_active=_yn(tp.get("text_to_sql_db_exists", True)),
        pk_columns=pk_columns,
        datasource=_ds_info(row.get("schema_props") or {}, row.get("ds_props") or {}),
        lineage_brief=LineageBrief(),
        ontology_anchors=[],
    )

    columns: List[ColumnMeta] = []
    for c in cols:
        cons: List[ColumnConstraint] = []
        if c.get("is_primary_key") is True:
            cons.append("PK")
        columns.append(
            ColumnMeta(
                column_name=str(c.get("name") or ""),
                column_name_kr=None,
                data_type=c.get("dtype"),
                column_comment=c.get("description"),
                constraints=cons,
                is_null=bool(c.get("nullable", True)),
                column_is_active="Y",
                code_lookup=None,
                term_mapping=None,
                value_examples=[],
            )
        )
    # FK 컬럼에 FK 제약 표시 보강
    fk_cols = {str(fk["from_column"]) for fk in fks if fk.get("from_column")}
    for cm in columns:
        if cm.column_name in fk_cols and "FK" not in cm.constraints:
            cm.constraints.append("FK")

    fk_list: List[RefMeta] = []
    for pos, fk in enumerate(fks, start=1):
        fk_list.append(
            RefMeta(
                column_name=str(fk["from_column"]),
                position=pos,
                ref_schema_name=str(fk.get("ref_schema_name") or ""),
                ref_table_name=str(fk.get("ref_table_name") or ""),
                ref_column_name=str(fk.get("ref_column_name") or ""),
            )
        )

    return MetaTableResponse(table_info=table_info, columns=columns, fk=fk_list)


# ---------------------------------------------------------------------------
# /meta/column
# ---------------------------------------------------------------------------
async def get_column(
    driver: AsyncDriver,
    *,
    db: Optional[str],
    schema_name: str,
    table_name: str,
    column_name: str,
) -> Optional[ColumnMeta]:
    """단일 컬럼 메타. PK/FK constraint 도 함께 채움."""
    cypher = f"""
    MATCH (t:Table)-[:{REL_TABLE_SCHEMA}]-(s:Schema)
    WHERE toLower(s.name) = toLower($schema_name)
      AND toLower(t.name) = toLower($table_name)
    OPTIONAL MATCH (ds:DataSource)-[:{REL_SCHEMA_DATASOURCE}]->(s)
    WITH t, s, ds, COALESCE(s.db, ds.engine, $default_db) AS db_label
    WHERE $db_filter IS NULL OR toLower(db_label) = toLower($db_filter)
    MATCH (t)-[:{REL_TABLE_COLUMN}]->(c:Column)
    WHERE toLower(c.name) = toLower($column_name)
    OPTIONAL MATCH (c)-[:{REL_COLUMN_FK}]->(c2:Column)
    RETURN c {{.*}} AS col, c2 IS NOT NULL AS is_fk
    LIMIT 1
    """
    async with driver.session() as sess:
        result = await sess.run(
            cypher,
            schema_name=schema_name,
            table_name=table_name,
            column_name=column_name,
            db_filter=db,
            default_db=DEFAULT_DB(),
        )
        row = await result.single()
    if row is None:
        return None
    c = row["col"] or {}
    cons: List[ColumnConstraint] = []
    if c.get("is_primary_key") is True:
        cons.append("PK")
    if row.get("is_fk"):
        cons.append("FK")
    return ColumnMeta(
        column_name=str(c.get("name") or column_name),
        column_name_kr=None,
        data_type=c.get("dtype"),
        column_comment=c.get("description"),
        constraints=cons,
        is_null=bool(c.get("nullable", True)),
        column_is_active="Y",
        code_lookup=None,
        term_mapping=None,
        value_examples=[],
    )


# ---------------------------------------------------------------------------
# /meta/ref
# ---------------------------------------------------------------------------
async def get_refs(
    driver: AsyncDriver,
    *,
    db: Optional[str],
    schema_name: str,
    table_name: str,
) -> List[RefMeta]:
    cypher = f"""
    MATCH (t1:Table)-[:{REL_TABLE_SCHEMA}]-(s1:Schema)
    WHERE toLower(s1.name) = toLower($schema_name)
      AND toLower(t1.name) = toLower($table_name)
    OPTIONAL MATCH (ds:DataSource)-[:{REL_SCHEMA_DATASOURCE}]->(s1)
    WITH t1, s1, ds, COALESCE(s1.db, ds.engine, $default_db) AS db_label
    WHERE $db_filter IS NULL OR toLower(db_label) = toLower($db_filter)
    MATCH (t1)-[:{REL_TABLE_COLUMN}]->(c1:Column)-[:{REL_COLUMN_FK}]->(c2:Column)<-[:{REL_TABLE_COLUMN}]-(t2:Table)
    MATCH (t2)-[:{REL_TABLE_SCHEMA}]-(s2:Schema)
    RETURN c1.name AS column_name,
           s2.name AS ref_schema_name,
           t2.name AS ref_table_name,
           c2.name AS ref_column_name
    ORDER BY column_name, ref_schema_name, ref_table_name
    """
    async with driver.session() as sess:
        result = await sess.run(
            cypher,
            schema_name=schema_name,
            table_name=table_name,
            db_filter=db,
            default_db=DEFAULT_DB(),
        )
        rows = await result.data()
    return [
        RefMeta(
            column_name=str(r["column_name"]),
            position=pos,
            ref_schema_name=str(r["ref_schema_name"]),
            ref_table_name=str(r["ref_table_name"]),
            ref_column_name=str(r["ref_column_name"]),
        )
        for pos, r in enumerate(rows, start=1)
    ]
