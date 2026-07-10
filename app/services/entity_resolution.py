"""v0.7 A안 — 1차 /data_decision entity/code 해소 (probe registry 기반)."""

from __future__ import annotations

import re
from typing import Any, List, Tuple

from neo4j import AsyncDriver

from ..config import settings
from ..schemas import ResolvedEntity, ResolvedValue, SuggestedProbe
from .entity_probe_registry import (
    load_probe_specs,
    probe_specs_to_columns,
    spec_for_column,
)
from .neo4j_client.db_probe import batch_db_probe, get_pg_pool

_MAX_RESOLVED_VALUES = 5


def _quoted_ident(part: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "", str(part or ""))
    return f'"{ident}"' if ident else ""


def _resolve_pg_schema(table_name: str, *, override: str = "") -> str:
    if override:
        return override.strip()
    name = (table_name or "").lower()
    if name.startswith("fct_"):
        return "mart"
    return (settings.source_pg_schema or "RWIS").strip() or "RWIS"


def _qualified_table_ref(table_name: str, *, pg_schema: str = "") -> str:
    table_q = _quoted_ident(table_name)
    if not table_q:
        return ""
    schema_q = _quoted_ident(_resolve_pg_schema(table_name, override=pg_schema))
    return f"{schema_q}.{table_q}" if schema_q else table_q


def _normalize_code(value: Any) -> str:
    return str(value or "").strip()


def _space_insensitive_match_sql(col_ident: str, kw_esc: str) -> str:
    return (
        f"replace({col_ident}::text, ' ', '') "
        f"ILIKE '%' || replace('{kw_esc}', ' ', '') || '%'"
    )


async def _lookup_code_label_pairs(
    pool,
    *,
    table_name: str,
    name_col: str,
    code_col: str,
    keyword: str,
    pg_schema: str = "",
    limit: int = 10,
) -> List[tuple[str, str]]:
    table_ref = _qualified_table_ref(table_name, pg_schema=pg_schema)
    name_q = _quoted_ident(name_col)
    code_q = _quoted_ident(code_col)
    if not all([table_ref, name_q, code_q]):
        return []
    kw_esc = keyword.replace("'", "''")
    sql = (
        f"SELECT rtrim({code_q}::text) AS code, {name_q}::text AS label "
        f"FROM {table_ref} "
        f"WHERE {name_q} IS NOT NULL AND {_space_insensitive_match_sql(name_q, kw_esc)} "
        f"ORDER BY {code_q} "
        f"LIMIT {int(limit)}"
    )
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql)
    except Exception:
        return []
    return _rows_to_code_label_pairs(rows)


async def _lookup_code_label_pairs_for_values(
    pool,
    *,
    table_name: str,
    name_col: str,
    code_col: str,
    names: List[str],
    pg_schema: str = "",
    limit: int = 10,
) -> List[tuple[str, str]]:
    table_ref = _qualified_table_ref(table_name, pg_schema=pg_schema)
    name_q = _quoted_ident(name_col)
    code_q = _quoted_ident(code_col)
    if not all([table_ref, name_q, code_q]):
        return []
    cleaned = [str(n).strip() for n in names if str(n or "").strip()]
    if not cleaned:
        return []
    in_list = ", ".join(f"'{n.replace(chr(39), chr(39)*2)}'" for n in cleaned[:limit])
    sql = (
        f"SELECT rtrim({code_q}::text) AS code, {name_q}::text AS label "
        f"FROM {table_ref} "
        f"WHERE {name_q}::text IN ({in_list}) "
        f"ORDER BY {code_q} "
        f"LIMIT {int(limit)}"
    )
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql)
    except Exception:
        return []
    return _rows_to_code_label_pairs(rows)


def _rows_to_code_label_pairs(rows) -> List[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        code = _normalize_code(row.get("code"))
        label = str(row.get("label") or "").strip()
        if not code or not label:
            continue
        key = f"{code}|{label}"
        if key in seen:
            continue
        seen.add(key)
        out.append((code, label))
    return out


_FACILITY_SUFFIXES = ("정수장", "사업장", "시설", "처리장", "정수")


def _entity_keywords(query: str, hyde_out: Any) -> List[str]:
    import re as _re

    kws: list[str] = []
    seen: set[str] = set()
    stop = {
        "알려줘",
        "보여줘",
        "검색",
        "조회",
        "해줘",
        "현황",
        "년",
        "월",
        "일",
        "데이터",
        "측정",
        "있는",
        "어떤",
        "있어",
        "뭐야",
        "단위가",
        "전체",
        "평균",
        "합계",
        "최대",
        "최소",
        "개수",
        "통계",
    }

    def _add(items):
        for x in items or []:
            s = str(x or "").strip()
            if len(s) >= 2 and s not in stop and s.lower() not in seen:
                seen.add(s.lower())
                kws.append(s)

    if hyde_out:
        _add(getattr(getattr(hyde_out, "entities", None), "include", None))
        measurement = getattr(hyde_out, "measurement", None)
        if measurement is not None:
            _add([getattr(measurement, "metric_meaning", None)])
            agg = str(getattr(measurement, "aggregation", None) or "").strip()
            if agg and agg.upper() not in {"AVG", "SUM", "COUNT", "MAX", "MIN"}:
                _add([agg])

    for m in _re.finditer(r"[가-힣]{2,}(?:정수장|사업장|처리장)", query):
        phrase = m.group(0)
        _add([phrase])
        core = phrase
        for suf in _FACILITY_SUFFIXES:
            if core.endswith(suf) and len(core) > len(suf):
                _add([core[: -len(suf)]])
                break

    for t in _re.findall(r"[가-힣]{2,}", query):
        if t not in stop:
            _add([t])

    for m in _re.finditer(r"[A-Za-z][A-Za-z0-9%./°\-]{1,}", query):
        token = m.group(0).strip()
        if len(token) >= 2 and token.lower() not in seen:
            seen.add(token.lower())
            kws.append(token)

    return kws[:12]


def _append_resolution(
    *,
    resolved: List[ResolvedEntity],
    probes: List[SuggestedProbe],
    kw: str,
    col,
    spec,
    code_col: str,
    code_values: List[ResolvedValue],
    pg_schema: str,
) -> None:
    entity_type = spec.entity_type if spec else "code"
    schema_name = spec.neo4j_schema if spec else (col.table_schema or "")

    if len(code_values) == 1:
        resolved.append(
            ResolvedEntity(
                mention=kw,
                entity_type=entity_type,
                schema_name=schema_name,
                table=col.table_name,
                name_column=col.name,
                code_column=code_col,
                values=code_values,
                source="db_probe",
            )
        )
        return

    if len(code_values) <= _MAX_RESOLVED_VALUES:
        resolved.append(
            ResolvedEntity(
                mention=kw,
                entity_type=entity_type,
                schema_name=schema_name,
                table=col.table_name,
                name_column=col.name,
                code_column=code_col,
                values=code_values,
                source="db_probe",
            )
        )
        reason = f"multiple matches ({len(code_values)}) for mention={kw}"
    else:
        reason = f"ambiguous matches for mention={kw}"

    table_ref = _qualified_table_ref(col.table_name, pg_schema=pg_schema)
    name_q = _quoted_ident(col.name)
    code_q = _quoted_ident(code_col)
    probes.append(
        SuggestedProbe(
            purpose="code_lookup",
            sql=(
                f"SELECT rtrim({code_q}::text) AS code, {name_q}::text AS label "
                f"FROM {table_ref} "
                            f"WHERE {_space_insensitive_match_sql(name_q, kw.replace(chr(39), chr(39)*2))} "
                f"LIMIT 10"
            ),
            expected_columns=[col.name, code_col],
            maps_to_filter={"column": code_col, "op": "="},
            reason=reason,
        )
    )


async def resolve_entities(
    driver: AsyncDriver,
    *,
    query: str,
    hyde_out: Any,
) -> Tuple[List[ResolvedEntity], List[SuggestedProbe], str]:
    """Returns (resolved_entities, suggested_probes, resolution_status)."""
    del driver  # A안: PG registry probe (Neo4j 컬럼 탐색 미사용)

    if not settings.entity_resolution_enabled:
        return [], [], "skipped"

    keywords = _entity_keywords(query, hyde_out)
    if not keywords:
        return [], [], "skipped"

    pool = await get_pg_pool()
    if pool is None:
        return [], [], "failed"

    try:
        specs = load_probe_specs()
    except (FileNotFoundError, ValueError):
        return [], [], "failed"

    probe_cols = probe_specs_to_columns(specs)
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
            spec = spec_for_column(specs, table_name=col.table_name, label_column=col.name)
            if not spec:
                continue
            code_col = spec.code_column
            pg_schema = spec.pg_schema

            pairs = await _lookup_code_label_pairs_for_values(
                pool,
                table_name=col.table_name,
                name_col=col.name,
                code_col=code_col,
                names=values,
                pg_schema=pg_schema,
                limit=10,
            )
            if not pairs:
                pairs = await _lookup_code_label_pairs(
                    pool,
                    table_name=col.table_name,
                    name_col=col.name,
                    code_col=code_col,
                    keyword=kw,
                    pg_schema=pg_schema,
                    limit=10,
                )

            code_values = [
                ResolvedValue(code=code, label=label, confidence=1.0)
                for code, label in pairs[:_MAX_RESOLVED_VALUES]
            ]
            if not code_values:
                continue

            _append_resolution(
                resolved=resolved,
                probes=probes,
                kw=kw,
                col=col,
                spec=spec,
                code_col=code_col,
                code_values=code_values,
                pg_schema=pg_schema,
            )

    if resolved and not probes:
        status = "complete"
    elif resolved or probes:
        status = "partial"
    else:
        status = "failed"

    return resolved, probes, status
