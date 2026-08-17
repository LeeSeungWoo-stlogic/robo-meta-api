"""Deterministic probe SQL and extra (schema, table) allow checks."""

from __future__ import annotations

import re

from ...schemas import ResolvedEntity
from ..decision_postgres.helpers import _resolve_subject_area
from ..sql_guard import GuardError
from ..sql_source_qualify import extract_sql_table_refs

_MASTER_AREAS = {"master", "code"}


def entity_needs_live_probe(entity: ResolvedEntity) -> bool:
    """Live LIKE probe is only for mentions that still have no resolved code.

    Approved value mappings and already-filled `values[].code` are SoT.
    Do not re-query a code table to confirm them.
    """
    if not (entity.table or "").strip() or not (entity.name_column or "").strip():
        return False
    return not any(str(item.code or "").strip() for item in entity.values)


def probe_allowlist(
    object_refs: list[dict],
    entities: list[ResolvedEntity],
) -> set[tuple[str, str]]:
    allowed: set[tuple[str, str]] = set()
    for item in object_refs:
        area = _resolve_subject_area(
            {
                "schema_name": item.get("schema_name"),
                "original_name": item.get("original_name"),
                "subject_area": item.get("subject_area"),
                "subject_area_override": item.get("subject_area_override"),
            }
        )
        if area not in _MASTER_AREAS:
            continue
        schema = str(item.get("schema_name") or "").lower()
        table = str(item.get("original_name") or "").lower()
        if schema and table:
            allowed.add((schema, table))
    for entity in entities:
        schema = str(entity.schema_name or "").lower()
        table = str(entity.table or "").lower()
        if schema and table:
            allowed.add((schema, table))
        elif table:
            allowed.add(("", table))
    return allowed


def assert_sql_in_allowlist(sql: str, allowed: set[tuple[str, str]]) -> None:
    if not allowed:
        raise GuardError("probe/validate allowlist가 비어 있습니다")
    for ref in extract_sql_table_refs(sql):
        schema = (ref.schema_name or "").lower()
        table = ref.table_name.lower()
        if schema:
            if (schema, table) not in allowed:
                raise GuardError(f"허용되지 않은 table: {schema}.{table}")
            continue
        if not any(item[1] == table for item in allowed):
            raise GuardError(f"허용되지 않은 table: {table}")


def quote_ident(name: str, *, fold_lower: bool = False) -> str:
    raw = str(name).replace("`", "")
    if fold_lower:
        raw = raw.lower()
    return "`" + raw + "`"


def build_entity_probe_sql(
    source_name: str,
    entity: ResolvedEntity,
    mention: str,
    *,
    limit: int,
    fold_lower: bool = False,
) -> str | None:
    table = (entity.table or "").strip()
    name_column = (entity.name_column or "").strip()
    if not table or not name_column:
        return None
    schema = (entity.schema_name or "").strip()
    ident = (
        f"{quote_ident(source_name, fold_lower=False)}"
        f".{quote_ident(schema, fold_lower=fold_lower)}"
        f".{quote_ident(table, fold_lower=fold_lower)}"
        if schema
        else (
            f"{quote_ident(source_name, fold_lower=False)}"
            f".{quote_ident(table, fold_lower=fold_lower)}"
        )
    )
    escaped = mention.replace("'", "''")
    return (
        f"SELECT {quote_ident(name_column, fold_lower=fold_lower)}"
        + (
            f", {quote_ident(entity.code_column, fold_lower=fold_lower)}"
            if entity.code_column
            else ""
        )
        + f" FROM {ident}"
        + f" WHERE {quote_ident(name_column, fold_lower=fold_lower)} LIKE '%{escaped}%'"
        + f" LIMIT {int(limit)}"
    )


_LIMIT_TAIL = re.compile(r"\bLIMIT\s+\d+\s*;?\s*$", re.IGNORECASE)


def with_limit(sql: str, limit: int) -> str:
    stripped = sql.strip().rstrip(";").strip()
    if _LIMIT_TAIL.search(stripped):
        return _LIMIT_TAIL.sub(f"LIMIT {int(limit)}", stripped)
    return f"{stripped} LIMIT {int(limit)}"
