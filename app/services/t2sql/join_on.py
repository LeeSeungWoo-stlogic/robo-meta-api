"""Approved JOIN ON predicates from query_plan.join_paths."""

from __future__ import annotations

from ...schemas import PlannedJoinPath
from .used_meta import used_table_keys


def _fqn_table_column(fqn: str) -> tuple[str, str] | None:
    parts = [part for part in fqn.split(".") if part]
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def missing_approved_on_predicates(
    sql: str,
    paths: list[PlannedJoinPath] | None,
) -> list[str]:
    """Return approved ON pairs missing when both tables are in the SQL.

    Generic: no physical table names. Composite keys are multiple conditions
    on the same path; each is required independently.
    """
    if not sql or not paths:
        return []
    used = {table.lower() for _, table in used_table_keys(sql) if table}
    sql_fold = sql.casefold()
    missing: list[str] = []
    seen: set[str] = set()
    for path in paths:
        for condition in path.conditions:
            left = _fqn_table_column(condition.from_)
            right = _fqn_table_column(condition.to)
            if left is None or right is None:
                continue
            left_table, left_column = left
            right_table, right_column = right
            if left_table.lower() not in used or right_table.lower() not in used:
                continue
            label = f"{condition.from_}={condition.to}"
            if label in seen:
                continue
            if (
                left_column.casefold() not in sql_fold
                or right_column.casefold() not in sql_fold
            ):
                seen.add(label)
                missing.append(label)
    return missing
