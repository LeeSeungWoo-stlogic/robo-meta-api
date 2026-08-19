"""Approved JOIN ON predicates from query_plan.join_paths, via sqlglot."""

from __future__ import annotations

from sqlglot import exp, parse_one

from ...schemas import PlannedJoinPath, QueryAnalysis, QueryPlan
from ..sql_guard import GuardError
from ..sql_source_qualify import promote_column_ident_literals
from .used_meta import used_table_keys

_PARSER = "mysql"
_HUB_ROLE_MARKERS = ("태그", "변량", "측정항목")


def _fqn_table_column(fqn: str) -> tuple[str, str] | None:
    parts = [part for part in fqn.split(".") if part]
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def _parse(sql: str) -> exp.Expression:
    try:
        tree = parse_one(sql, read=_PARSER)
    except Exception as exc:
        raise GuardError(f"SQL parser validation failed: {exc}") from exc
    promote_column_ident_literals(tree)
    return tree


def _alias_tables(tree: exp.Expression) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        name = str(table.name or "").lower()
        if not name:
            continue
        mapped[name] = name
        alias = str(table.alias_or_name or "").lower()
        if alias:
            mapped[alias] = name
    return mapped


def _column_table_name(
    node: exp.Expression,
    aliases: dict[str, str],
) -> tuple[str, str] | None:
    if not isinstance(node, exp.Column):
        return None
    column = str(node.name or "").lower()
    if not column:
        return None
    table = str(node.table or "").lower()
    if not table:
        return None
    return aliases.get(table, table), column


def sql_eq_join_pairs(sql: str) -> set[frozenset[tuple[str, str]]]:
    """ON equality pairs as frozenset({(table, col), (table, col)})."""

    tree = _parse(sql)
    aliases = _alias_tables(tree)
    pairs: set[frozenset[tuple[str, str]]] = set()
    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        if on is None:
            continue
        for eq in on.find_all(exp.EQ):
            left = _column_table_name(eq.left, aliases)
            right = _column_table_name(eq.right, aliases)
            if left is None or right is None:
                continue
            pairs.add(frozenset({left, right}))
    return pairs


def missing_approved_on_predicates(
    sql: str,
    paths: list[PlannedJoinPath] | None,
) -> list[str]:
    """Return approved ON pairs missing when both tables are in the SQL.

    Matches table pair, column pair, and equality. Column names on a
    different table pair do not count.
    """
    if not sql or not paths:
        return []
    used = {table.lower() for _, table in used_table_keys(sql) if table}
    try:
        present = sql_eq_join_pairs(sql)
    except GuardError:
        return ["SQL parse"]
    missing: list[str] = []
    seen: set[str] = set()
    for path in paths:
        for condition in path.conditions:
            left = _fqn_table_column(condition.from_)
            right = _fqn_table_column(condition.to)
            if left is None or right is None:
                continue
            left_table, left_column = left[0].lower(), left[1].lower()
            right_table, right_column = right[0].lower(), right[1].lower()
            if left_table not in used or right_table not in used:
                continue
            label = f"{condition.from_}={condition.to}"
            if label in seen:
                continue
            wanted = frozenset(
                {(left_table, left_column), (right_table, right_column)}
            )
            if wanted not in present:
                seen.add(label)
                missing.append(label)
    return missing


def _statement_is_single_select(sql: str) -> bool:
    tree = _parse(sql)
    if isinstance(tree, exp.Select):
        return True
    if isinstance(tree, exp.With) and isinstance(tree.this, exp.Select):
        return True
    return False


def _resolved_code_values(plan: QueryPlan | None) -> list[str]:
    if plan is None:
        return []
    values: list[str] = []
    for item in plan.filters:
        if item.resolution_status != "resolved":
            continue
        if item.operator not in {"EQ", "IN", "NE", "NOT_IN"}:
            continue
        for part in str(item.value or "").split(","):
            code = part.strip().strip("'\"")
            if code:
                values.append(code)
    return values


def _role_is_measure_hub(role: str) -> bool:
    compact = "".join(str(role or "").split())
    return any(marker in compact for marker in _HUB_ROLE_MARKERS)


def _list_hub_join_tables(sql: str, plan: QueryPlan, analysis: QueryAnalysis | None) -> list[str]:
    if str(getattr(analysis, "procedure", "") or "").strip() != "list":
        return []
    axis = " ".join(plan.answer_axis or [])
    axis_compact = "".join(axis.split())
    hub_names: set[str] = set()
    for table in plan.required_tables:
        role = " ".join([table.role, *list(table.roles or [])])
        if not _role_is_measure_hub(role):
            continue
        if axis_compact and any(marker in axis_compact for marker in _HUB_ROLE_MARKERS):
            continue
        hub_names.add(str(table.table_name or "").lower())
    if not hub_names:
        return []
    used = {table.lower() for _, table in used_table_keys(sql) if table}
    tree = _parse(sql)
    aliases = _alias_tables(tree)
    joined: list[str] = []
    for join in tree.find_all(exp.Join):
        target = join.this
        name = ""
        if isinstance(target, exp.Table):
            name = str(target.name or "").lower()
        elif isinstance(target, exp.Alias) and isinstance(target.this, exp.Table):
            name = str(target.this.name or "").lower()
        if name in hub_names and name in used:
            joined.append(name)
        for column in (join.args.get("on") or exp.true()).find_all(exp.Column):
            table = aliases.get(str(column.table or "").lower(), "")
            if table in hub_names:
                joined.append(table)
    return sorted(set(joined))


def _select_has_axis_table(sql: str, plan: QueryPlan, analysis: QueryAnalysis | None) -> bool:
    if str(getattr(analysis, "procedure", "") or "").strip() != "list":
        return True
    axis = [str(item) for item in (plan.answer_axis or []) if str(item).strip()]
    if not axis:
        return True
    used = {table.lower() for _, table in used_table_keys(sql) if table}
    for table in plan.required_tables:
        role = str(table.role or "")
        name = str(table.table_name or "").lower()
        if any(token in role for token in axis) or any(role in token for token in axis):
            return name in used
    return True


def guard_generated_sql(
    sql: str,
    plan: QueryPlan | None,
    analysis: QueryAnalysis | None,
) -> str | None:
    """Return a guard reason, or None when the AST is acceptable."""

    if not sql:
        return "빈 SQL"
    if not _statement_is_single_select(sql):
        return "단일 SELECT만 허용"
    missing_on = missing_approved_on_predicates(
        sql, plan.join_paths if plan else []
    )
    if missing_on:
        return "승인 JOIN ON이 빠짐: " + ",".join(missing_on)
    codes = _resolved_code_values(plan)
    if codes:
        for code in codes:
            quoted = f"'{code}'"
            if quoted not in sql and f'"{code}"' not in sql:
                return "resolved 코드 필터가 SQL에 없음"
    if plan is not None:
        hubs = _list_hub_join_tables(sql, plan, analysis)
        if hubs:
            return "목록 질의에 측정 허브 JOIN: " + ",".join(hubs)
        if not _select_has_axis_table(sql, plan, analysis):
            return "목록 답 축 표가 SELECT에 없음"
    return None
