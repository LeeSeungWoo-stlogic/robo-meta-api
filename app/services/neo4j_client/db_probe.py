"""
PostgreSQL db_probe — K-AIR robo-data-text2sql/app/react/tools/build_sql_context_parts/db_probe.py 기반.
MindsDB 경로 제거, PostgreSQL 전용으로 경량화.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Dict, List, Optional

import asyncpg

from .config import settings
from .models import ColumnCandidate


# ---------------------------------------------------------------------------
# DB 연결 풀 관리
# ---------------------------------------------------------------------------

_pool: Optional[asyncpg.Pool] = None


async def get_pg_pool() -> Optional[asyncpg.Pool]:
    global _pool
    if _pool is not None:
        return _pool
    try:
        schemas = [s.strip() for s in (settings.pg_schemas or "").split(",") if s.strip()]
        schemas_str = ", ".join(schemas)

        async def _init(conn: asyncpg.Connection) -> None:
            if schemas:
                quoted = ", ".join(
                    f'"{s.replace(chr(34), "")}"' for s in schemas
                )
                await conn.execute(f"SET search_path TO {quoted}")

        _pool = await asyncpg.create_pool(
            host=settings.pg_host,
            port=settings.pg_port,
            database=settings.pg_database,
            user=settings.pg_user,
            password=settings.pg_password,
            min_size=1,
            max_size=5,
            init=_init,
        )
        return _pool
    except Exception as exc:
        print(f"[WARN] PostgreSQL connection failed: {exc}")
        return None


async def close_pg_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# 단일 컬럼 probe (K-AIR 원본 로직, PostgreSQL 전용)
# ---------------------------------------------------------------------------

def _safe_ident(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", str(part or ""))


def _qualified_table_ident(table_name: str) -> str:
    """search_path 비의존 — RWIS/mart 스키마 명시."""
    table_id = _safe_ident(table_name)
    if not table_id:
        return ""
    if table_name.lower().startswith("fct_"):
        schema_id = "mart"
    else:
        schema_id = _safe_ident(
            os.getenv("SOURCE_PG_SCHEMA", "RWIS") or "RWIS"
        ) or "RWIS"
    return f'"{schema_id}"."{table_id}"'


def _space_insensitive_match_sql(col_ident: str, kw_esc: str) -> str:
    """PG ILIKE — 공백 무시 (예: 탁도 ↔ 탁 도)."""
    return (
        f"replace({col_ident}::text, ' ', '') "
        f"ILIKE '%' || replace('{kw_esc}', ' ', '') || '%'"
    )


async def limited_db_probe(
    *,
    keyword: str,
    column: ColumnCandidate,
    pool: asyncpg.Pool,
    timeout_s: float | None = None,
    value_limit: int | None = None,
) -> List[str]:
    """
    특정 컬럼에서 keyword ILIKE 검색.
    K-AIR _limited_db_probe 원본 로직.
    """
    kw = (keyword or "").strip()
    if not kw:
        return []

    timeout_s = timeout_s or settings.db_probe_timeout_s
    value_limit = value_limit or settings.db_probe_value_limit

    schema_id = _safe_ident(column.table_schema)
    table_id = _safe_ident(column.table_name)
    col_id = _safe_ident(column.name)
    if not table_id or not col_id:
        return []

    kw_esc = kw.replace("'", "''")
    table_ident = _qualified_table_ident(column.table_name)
    if not table_ident:
        return []
    col_ident = f'"{col_id}"'
    probe_sql = (
        f"SELECT DISTINCT {col_ident} AS value "
        f"FROM {table_ident} "
        f"WHERE {col_ident} IS NOT NULL AND {_space_insensitive_match_sql(col_ident, kw_esc)} "
        f"LIMIT {int(value_limit)}"
    )

    try:
        async with pool.acquire() as conn:
            rows = await asyncio.wait_for(conn.fetch(probe_sql), timeout=timeout_s)
    except Exception:
        return []

    out: List[str] = []
    for row in rows[:value_limit]:
        v = row.get("value") if hasattr(row, "get") else row[0]
        if v is not None:
            s = str(v).strip()
            if s:
                out.append(s)
    return out[:value_limit]


# ---------------------------------------------------------------------------
# 배치 probe: 여러 키워드 × 여러 컬럼 교차 검색
# ---------------------------------------------------------------------------

async def batch_db_probe(
    *,
    keywords: List[str],
    columns: List[ColumnCandidate],
) -> Dict[str, Dict[str, List[str]]]:
    """
    Returns: { keyword: { column_fqn: [matched_values] } }
    PostgreSQL 미연결 시 빈 dict 반환.

    키워드별 순차 처리 — 다수 키워드×컬럼 동시 실행 시 PG 풀(5) 포화로
    probe 타임아웃·누락이 발생하는 것을 방지.
    """
    pool = await get_pg_pool()
    if pool is None:
        return {}

    results: Dict[str, Dict[str, List[str]]] = {}

    for kw in keywords:
        kw_stripped = (kw or "").strip()
        if not kw_stripped:
            continue
        kw_map: Dict[str, List[str]] = {}
        for col in columns:
            vals = await limited_db_probe(keyword=kw_stripped, column=col, pool=pool)
            if vals:
                kw_map[col.column_fqn] = vals
        if kw_map:
            results[kw_stripped] = kw_map

    return results
