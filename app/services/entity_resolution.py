"""v0.7 A안 — 1차 /data_decision entity/code 해소."""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

from neo4j import AsyncDriver

from ..config import settings
from ..schemas import ResolvedEntity, ResolvedValue, SuggestedProbe
from .neo4j_client.db_probe import batch_db_probe, get_pg_pool
from .neo4j_client.models import ColumnCandidate


_NAME_SUFFIXES = ("_name", "_nm", "name")
_CODE_SUFFIXES = ("_code", "_cd", "_id", "code")


def _entity_keywords(query: str, hyde_out: Any) -> List[str]:
    import re as _re

    kws: list[str] = []
    seen: set[str] = set()
    stop = {"알려줘", "보여줘", "검색", "조회", "해줘", "현황", "년", "월", "일"}

    def _add(items):
        for x in items or []:
            s = str(x or "").strip()
            if len(s) >= 2 and s not in stop and s.lower() not in seen:
                seen.add(s.lower())
                kws.append(s)

    if hyde_out:
        _add(getattr(getattr(hyde_out, "entities", None), "include", None))
    for t in _re.findall(r"[가-힣]{2,}", query):
        if t not in stop:
            _add([t])
    return kws[:8]


async def _fetch_master_probe_columns(driver: AsyncDriver) -> List[ColumnCandidate]:
    cypher = """
    MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)
    WHERE COALESCE(t.subject_area, '') IN ['master', 'code']
      AND COALESCE(t.text_to_sql_db_exists, true) = true
      AND toLower(c.name) CONTAINS 'name'
    RETURN t.schema AS table_schema, t.name AS table_name, c.name AS name,
           coalesce(c.dtype, '') AS dtype, coalesce(c.description, '') AS description
    LIMIT 80
    """
    async with driver.session() as sess:
        res = await sess.run(cypher)
        rows = await res.data()
    return [
        ColumnCandidate(
            table_schema=str(r.get("table_schema") or ""),
            table_name=str(r.get("table_name") or ""),
            name=str(r.get("name") or ""),
            dtype=str(r.get("dtype") or ""),
            description=str(r.get("description") or ""),
        )
        for r in rows
    ]


def _guess_code_column(name_col: str, all_cols: List[str]) -> Optional[str]:
    base = name_col.lower()
    for suffix in _NAME_SUFFIXES:
        if base.endswith(suffix):
            stem = base[: -len(suffix)]
            for cs in _CODE_SUFFIXES:
                cand = stem + cs
                if cand in {c.lower() for c in all_cols}:
                    for c in all_cols:
                        if c.lower() == cand:
                            return c
    for c in all_cols:
        cl = c.lower()
        if cl.endswith("_code") or cl.endswith("_cd"):
            return c
    return None


async def _table_columns(driver: AsyncDriver, schema: str, table: str) -> List[str]:
    cypher = """
    MATCH (t:Table {schema: $schema, name: $table})-[:HAS_COLUMN]->(c:Column)
    RETURN c.name AS name
    """
    async with driver.session() as sess:
        res = await sess.run(cypher, schema=schema, table=table)
        rows = await res.data()
    return [str(r["name"]) for r in rows if r.get("name")]


async def resolve_entities(
    driver: AsyncDriver,
    *,
    query: str,
    hyde_out: Any,
) -> Tuple[List[ResolvedEntity], List[SuggestedProbe], str]:
    """Returns (resolved_entities, suggested_probes, resolution_status)."""
    if not settings.entity_resolution_enabled:
        return [], [], "skipped"

    keywords = _entity_keywords(query, hyde_out)
    if not keywords:
        return [], [], "skipped"

    pool = await get_pg_pool()
    if pool is None:
        return [], [], "failed"

    probe_cols = await _fetch_master_probe_columns(driver)
    if not probe_cols:
        return [], [], "skipped"

    probe_results = await batch_db_probe(keywords=keywords, columns=probe_cols)
    resolved: List[ResolvedEntity] = []
    probes: List[SuggestedProbe] = []

    for kw, col_map in probe_results.items():
        for fqn, values in col_map.items():
            if not values:
                continue
            col = next((c for c in probe_cols if c.column_fqn == fqn), None)
            if not col:
                continue
            all_cols = await _table_columns(driver, col.table_schema, col.table_name)
            code_col = _guess_code_column(col.name, all_cols)
            entity_type = "facility" if "suj" in col.table_name.lower() or "plant" in col.table_name.lower() else "code"

            if len(values) == 1 or (len(values) <= 3 and code_col):
                code_values: list[ResolvedValue] = []
                if code_col and len(values) == 1:
                    label = values[0]
                    schema_id = re.sub(r"[^A-Za-z0-9_]", "", col.table_schema)
                    table_id = re.sub(r"[^A-Za-z0-9_]", "", col.table_name)
                    col_id = re.sub(r"[^A-Za-z0-9_]", "", code_col)
                    probe_sql = (
                        f'SELECT "{col_id}" AS code, "{re.sub(r"[^A-Za-z0-9_]", "", col.name)}" AS label '
                        f'FROM "{schema_id}"."{table_id}" '
                        f'WHERE "{re.sub(r"[^A-Za-z0-9_]", "", col.name)}"::text ILIKE \'%{label.replace(chr(39), chr(39)*2)}%\' '
                        f"LIMIT 5"
                    )
                    try:
                        async with pool.acquire() as conn:
                            rows = await conn.fetch(probe_sql)
                        for row in rows:
                            code_values.append(
                                ResolvedValue(
                                    code=str(row["code"]),
                                    label=str(row.get("label") or label),
                                    confidence=1.0,
                                )
                            )
                    except Exception:
                        code_values.append(ResolvedValue(code=values[0], label=values[0], confidence=0.7))
                else:
                    for v in values[:3]:
                        code_values.append(ResolvedValue(code=v, label=v, confidence=0.8))

                resolved.append(
                    ResolvedEntity(
                        mention=kw,
                        entity_type=entity_type,
                        schema_name=col.table_schema,
                        table=col.table_name,
                        name_column=col.name,
                        code_column=code_col,
                        values=code_values,
                        source="db_probe",
                    )
                )
            else:
                schema_id = re.sub(r"[^A-Za-z0-9_]", "", col.table_schema)
                table_id = re.sub(r"[^A-Za-z0-9_]", "", col.table_name)
                col_id = re.sub(r"[^A-Za-z0-9_]", "", col.name)
                sql = (
                    f'SELECT DISTINCT "{col_id}" FROM "{schema_id}"."{table_id}" '
                    f"WHERE \"{col_id}\"::text ILIKE '%{kw.replace(chr(39), chr(39)*2)}%' LIMIT 10"
                )
                probes.append(
                    SuggestedProbe(
                        purpose="code_lookup",
                        sql=sql,
                        expected_columns=[col.name, code_col] if code_col else [col.name],
                        maps_to_filter={"column": code_col or col.name, "op": "="},
                        reason=f"ambiguous matches for mention={kw}",
                    )
                )

    if resolved and not probes:
        status = "complete"
    elif resolved:
        status = "partial"
    elif probes:
        status = "partial"
    else:
        status = "failed"

    return resolved, probes, status
