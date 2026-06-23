"""
Neo4j 벡터 검색 — K-AIR robo-data-text2sql/app/react/tools/build_sql_context_parts/neo4j.py 기반.
핵심 함수만 추출하여 독립 모듈로 구성.
"""
from __future__ import annotations

import traceback
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import ColumnCandidate, TableCandidate

from ...config import settings
from ..kair_graph_adapter import REL_COLUMN_FK, REL_TABLE_COLUMN


def _is_missing_vector_index_error(exc: Exception, *, index_name: str) -> bool:
    msg = str(exc or "")
    return ("no such vector schema index" in msg.lower()) and (index_name in msg)


# ---------------------------------------------------------------------------
# 테이블 벡터 검색 (K-AIR 원본)
# ---------------------------------------------------------------------------

async def search_tables_by_vector(
    session,
    *,
    embedding: List[float],
    k: int = 15,
    schema_filter: Optional[Sequence[str]] = None,
) -> Tuple[List[TableCandidate], str]:
    """Table.text_to_sql_vector 벡터 인덱스 검색 → fallback: cosine scan."""
    k = max(1, int(k))
    schema_filter_lower = [s.strip().lower() for s in (schema_filter or []) if str(s or "").strip()]

    cypher_index = """
    CALL db.index.vector.queryNodes($index_name, $k, $embedding)
    YIELD node, score
    WITH node, score
    WHERE node:Table AND node.text_to_sql_vector IS NOT NULL AND size(node.text_to_sql_vector) > 0
      AND COALESCE(node.text_to_sql_db_exists, true) = true
      AND ($schemas IS NULL OR toLower(COALESCE(node.schema,'')) IN $schemas)
    RETURN
      COALESCE(node.schema,'') AS schema,
      COALESCE(node.name,'') AS name,
      COALESCE(node.description,'') AS description,
      COALESCE(node.analyzed_description,'') AS analyzed_description,
      COALESCE(node.datasource,'') AS datasource,
      COALESCE(node.subject_area,'') AS subject_area,
      score AS score
    ORDER BY score DESC, schema ASC, name ASC
    LIMIT $k
    """
    try:
        res = await session.run(
            cypher_index,
            index_name=settings.neo4j_table_vector_index,
            k=k,
            embedding=embedding,
            schemas=(schema_filter_lower or None),
        )
        rows = await res.data()
        out = [
            TableCandidate(
                schema=str(r.get("schema") or ""),
                name=str(r.get("name") or ""),
                description=str(r.get("description") or ""),
                analyzed_description=str(r.get("analyzed_description") or ""),
                datasource=str(r.get("datasource") or ""),
                subject_area=str(r.get("subject_area") or ""),
                score=float(r.get("score") or 0.0),
            )
            for r in rows
        ]
        return out, "text2sql_vec_index"
    except Exception as exc:
        if not _is_missing_vector_index_error(exc, index_name=settings.neo4j_table_vector_index):
            print(f"[WARN] vector index query failed: {exc}")

    # Fallback: cosine similarity scan
    cypher_scan = """
    MATCH (t:Table)
    WHERE t.text_to_sql_vector IS NOT NULL AND size(t.text_to_sql_vector) > 0
      AND COALESCE(t.text_to_sql_db_exists, true) = true
      AND ($schemas IS NULL OR toLower(COALESCE(t.schema,'')) IN $schemas)
    WITH t, vector.similarity.cosine(t.text_to_sql_vector, $embedding) AS score
    RETURN
      COALESCE(t.schema,'') AS schema,
      COALESCE(t.name,'') AS name,
      COALESCE(t.description,'') AS description,
      COALESCE(t.analyzed_description,'') AS analyzed_description,
      COALESCE(t.datasource,'') AS datasource,
      COALESCE(t.subject_area,'') AS subject_area,
      score AS score
    ORDER BY score DESC, schema ASC, name ASC
    LIMIT $k
    """
    res = await session.run(
        cypher_scan,
        k=k,
        embedding=embedding,
        schemas=(schema_filter_lower or None),
    )
    rows = await res.data()
    out2 = [
        TableCandidate(
            schema=str(r.get("schema") or ""),
            name=str(r.get("name") or ""),
            description=str(r.get("description") or ""),
            analyzed_description=str(r.get("analyzed_description") or ""),
            datasource=str(r.get("datasource") or ""),
            subject_area=str(r.get("subject_area") or ""),
            score=float(r.get("score") or 0.0),
        )
        for r in rows
    ]
    return out2, "text2sql_vec_scan_fallback"


# ---------------------------------------------------------------------------
# 테이블 스키마 조회 (K-AIR 원본)
# ---------------------------------------------------------------------------

async def fetch_table_schemas(
    session,
    *,
    tables: Sequence[TableCandidate],
) -> List[Dict[str, Any]]:
    requested = []
    for t in tables:
        name = (t.name or "").strip()
        schema = (t.schema or "").strip()
        if not name:
            continue
        requested.append({"schema": schema.lower() if schema else None, "name": name.lower()})
    if not requested:
        return []

    cypher = f"""
    UNWIND $requested AS req
    MATCH (t:Table)
    WHERE (
      (t.name IS NOT NULL AND toLower(t.name) = req.name)
      OR (t.original_name IS NOT NULL AND toLower(t.original_name) = req.name)
    )
      AND (req.schema IS NULL OR (t.schema IS NOT NULL AND toLower(t.schema) = req.schema))
      AND COALESCE(t.text_to_sql_db_exists, true) = true
    WITH DISTINCT t
    OPTIONAL MATCH (t)-[:{REL_TABLE_COLUMN}]->(c:Column)
    WITH t, c
    ORDER BY c.name
    RETURN COALESCE(t.original_name, t.name) AS table_name,
           t.schema AS table_schema,
           t.description AS table_description,
           t.text_to_sql_table_type AS table_type,
           t.datasource AS datasource,
           collect({{
               name: c.name,
               fqn: c.fqn,
               dtype: c.dtype,
               nullable: c.nullable,
               description: c.description,
               is_primary_key: c.is_primary_key,
               enum_values: c.enum_values,
               cardinality: c.cardinality
           }}) AS columns
    ORDER BY table_schema, table_name
    """
    res = await session.run(cypher, requested=requested)
    records = await res.data()
    return [
        {
            "schema": str(r.get("table_schema") or ""),
            "name": str(r.get("table_name") or ""),
            "description": str(r.get("table_description") or ""),
            "table_type": str(r.get("table_type") or ""),
            "datasource": str(r.get("datasource") or ""),
            "columns": r.get("columns") or [],
        }
        for r in records
    ]


# ---------------------------------------------------------------------------
# FK 관계 조회 (K-AIR 원본)
# ---------------------------------------------------------------------------

async def fetch_fk_relationships(
    session,
    *,
    table_fqns: Sequence[str],
    limit: int = 30,
) -> List[Dict[str, Any]]:
    if not table_fqns:
        return []
    table_fqns_l = [str(x or "").strip().lower() for x in table_fqns if str(x or "").strip()]
    cypher = f"""
    MATCH (t1:Table)-[:{REL_TABLE_COLUMN}]->(c1:Column)-[fk:{REL_COLUMN_FK}]->(c2:Column)<-[:{REL_TABLE_COLUMN}]-(t2:Table)
    WITH t1, c1, fk, c2, t2,
         (CASE WHEN t1.schema IS NOT NULL AND t1.schema <> '' THEN toLower(t1.schema) + '.' + toLower(t1.name) ELSE toLower(t1.name) END) AS fqn1,
         (CASE WHEN t2.schema IS NOT NULL AND t2.schema <> '' THEN toLower(t2.schema) + '.' + toLower(t2.name) ELSE toLower(t2.name) END) AS fqn2
    WHERE fqn1 IN $table_fqns AND fqn2 IN $table_fqns
    RETURN t1.name AS from_table,
           t1.schema AS from_schema,
           c1.name AS from_column,
           t2.name AS to_table,
           t2.schema AS to_schema,
           c2.name AS to_column,
           (CASE WHEN 'constraint' IN keys(fk) THEN properties(fk)['constraint'] ELSE '' END) AS constraint_name
    ORDER BY from_schema, from_table, to_schema, to_table
    LIMIT $limit
    """
    res = await session.run(cypher, table_fqns=table_fqns_l, limit=int(limit))
    return await res.data()


# ---------------------------------------------------------------------------
# 온톨로지 / convention JOIN (v3 2·3단 fallback)
# ---------------------------------------------------------------------------


def _normalize_dtype(dtype: str | None) -> str:
    d = str(dtype or "").lower().strip()
    return d.split("(")[0].strip()


def _convention_bridge_confidence(
    from_dtype: str | None,
    to_dtype: str | None,
    *,
    match_conf: float,
    mismatch_conf: float,
) -> tuple[float, bool]:
    nf, nt = _normalize_dtype(from_dtype), _normalize_dtype(to_dtype)
    if nf and nt:
        return (match_conf, True) if nf == nt else (mismatch_conf, False)
    return mismatch_conf, False


def build_convention_bridge_path(
    col: str,
    from_dtype: str | None,
    to_dtype: str | None,
    *,
    dtype_match: bool,
) -> List[str]:
    path = [f"shared_column:{col}"]
    fd = from_dtype or "unknown"
    td = to_dtype or "unknown"
    path.append(f"dtype:{fd}↔{td}")
    if not dtype_match:
        path.append("cast_recommended:true")
    return path


async def fetch_ontology_relationships(
    session,
    *,
    table_fqns: Sequence[str],
    limit: int = 50,
) -> List[Dict[str, Any]]:
    if not table_fqns:
        return []
    if not settings.join_ontology_rel_types:
        return []

    cypher = """
    MATCH (t1:Table)-[r]-(t2:Table)
    WHERE type(r) IN $rel_types
    WITH t1, t2, r,
         (CASE WHEN t1.schema IS NOT NULL AND t1.schema <> ''
               THEN toLower(t1.schema) + '.' + toLower(t1.name)
               ELSE toLower(t1.name) END) AS fqn1,
         (CASE WHEN t2.schema IS NOT NULL AND t2.schema <> ''
               THEN toLower(t2.schema) + '.' + toLower(t2.name)
               ELSE toLower(t2.name) END) AS fqn2
    WHERE fqn1 IN $fqns AND fqn2 IN $fqns AND fqn1 < fqn2
    RETURN
      COALESCE(t1.schema,'') AS from_schema,
      COALESCE(t1.name,'')   AS from_table,
      COALESCE(t2.schema,'') AS to_schema,
      COALESCE(t2.name,'')   AS to_table,
      type(r)                AS rel_type,
      COALESCE(r.confidence, 0.8) AS confidence
    LIMIT $limit
    """
    fqns_lower = [str(f).lower() for f in table_fqns]
    result = await session.run(
        cypher,
        fqns=fqns_lower,
        rel_types=list(settings.join_ontology_rel_types),
        limit=int(limit),
    )
    return await result.data()


def _is_mart_fact_table(table_name: str) -> bool:
    """mart fact 테이블 식별 (Neo4j schema와 무관하게 fct_ prefix 기준)."""
    return str(table_name or "").lower().startswith("fct_")


async def fetch_convention_joins(
    session,
    *,
    table_fqns: Sequence[str],
    min_col_len: int = 4,
    excludes: set[str] | None = None,
    limit: int = 30,
    exclude_mart_pairs: bool = True,
) -> List[Dict[str, Any]]:
    if not table_fqns or len(table_fqns) < 2:
        return []

    ex = excludes if excludes is not None else settings.join_convention_exclude

    cypher = f"""
    UNWIND $fqns AS fqn1
    UNWIND $fqns AS fqn2
    WITH fqn1, fqn2 WHERE fqn1 < fqn2
    MATCH (t1:Table)-[:{REL_TABLE_COLUMN}]->(c1:Column)
    WHERE (CASE WHEN t1.schema IS NOT NULL AND t1.schema <> ''
                THEN toLower(t1.schema) + '.' + toLower(t1.name)
                ELSE toLower(t1.name) END) = fqn1
    MATCH (t2:Table)-[:{REL_TABLE_COLUMN}]->(c2:Column)
    WHERE (CASE WHEN t2.schema IS NOT NULL AND t2.schema <> ''
                THEN toLower(t2.schema) + '.' + toLower(t2.name)
                ELSE toLower(t2.name) END) = fqn2
      AND toLower(c1.name) = toLower(c2.name)
      AND size(c1.name) >= $min_len
      AND NOT toLower(c1.name) IN $excludes
    WITH fqn1, fqn2, t1, t2,
         c1.name AS join_column,
         COALESCE(c1.dtype, '') AS from_dtype,
         COALESCE(c2.dtype, '') AS to_dtype
    ORDER BY size(join_column) DESC
    WITH fqn1, fqn2, t1, t2,
         collect(join_column)[0] AS join_column,
         collect(from_dtype)[0] AS from_dtype,
         collect(to_dtype)[0] AS to_dtype
    RETURN
      COALESCE(t1.schema,'') AS from_schema,
      COALESCE(t1.name,'')   AS from_table,
      join_column,
      from_dtype,
      to_dtype,
      COALESCE(t2.schema,'') AS to_schema,
      COALESCE(t2.name,'')   AS to_table
    LIMIT $limit
    """
    fqns_lower = [str(f).lower() for f in table_fqns]
    result = await session.run(
        cypher,
        fqns=fqns_lower,
        min_len=int(min_col_len),
        excludes=list(ex),
        limit=int(limit),
    )
    rows = await result.data()
    if not exclude_mart_pairs:
        return rows
    return [
        r
        for r in rows
        if not (
            _is_mart_fact_table(str(r.get("from_table") or ""))
            and _is_mart_fact_table(str(r.get("to_table") or ""))
        )
    ]


# ---------------------------------------------------------------------------
# 테이블 컬럼 중 앵커 매칭 컬럼 조회 (K-AIR 원본)
# ---------------------------------------------------------------------------

async def fetch_anchor_columns(
    session,
    *,
    tables: Sequence[TableCandidate],
    keywords_lower: Sequence[str],
    per_table_limit: int = 10,
) -> List[ColumnCandidate]:
    requested = []
    for t in tables:
        name = (t.name or "").strip()
        schema = (t.schema or "").strip()
        if not name:
            continue
        requested.append({"schema": schema.lower() if schema else None, "name": name.lower()})
    if not requested:
        return []

    kws = [str(k or "").strip().lower() for k in keywords_lower if str(k or "").strip()][:20]
    if not kws:
        return []

    cypher = f"""
    UNWIND $requested AS req
    MATCH (t:Table)-[:{REL_TABLE_COLUMN}]->(c:Column)
    WHERE (
      (t.name IS NOT NULL AND toLower(t.name) = req.name)
      OR (t.original_name IS NOT NULL AND toLower(t.original_name) = req.name)
    )
      AND (req.schema IS NULL OR (t.schema IS NOT NULL AND toLower(t.schema) = req.schema))
      AND (
        any(kw IN $kws WHERE
          (c.name IS NOT NULL AND toLower(c.name) CONTAINS kw)
          OR (c.description IS NOT NULL AND toLower(c.description) CONTAINS kw)
        )
      )
    WITH req, t, c
    ORDER BY c.name
    WITH req, t, collect(c)[0..$per_table_limit] AS cols
    UNWIND cols AS c
    RETURN COALESCE(t.schema,'') AS table_schema,
           COALESCE(t.original_name, t.name) AS table_name,
           c.name AS name,
           c.dtype AS dtype,
           c.description AS description
    ORDER BY table_schema, table_name, name
    """
    res = await session.run(
        cypher, requested=requested, kws=kws, per_table_limit=int(per_table_limit)
    )
    rows = await res.data()
    return [
        ColumnCandidate(
            table_schema=str(r.get("table_schema") or ""),
            table_name=str(r.get("table_name") or ""),
            name=str(r.get("name") or ""),
            dtype=str(r.get("dtype") or ""),
            description=str(r.get("description") or ""),
            score=0.5,
        )
        for r in rows
    ]
