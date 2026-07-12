"""Artifact allowlist SQL guard (플랜 6단계).

published Semantic View Artifact가 허용한 table·column·join edge·mandatory
filter를 실행 SQL이 지키는지 검사한다. 기존 sql_guard(read-only·다중문 차단)
통과 후 추가로 적용된다.

semantic-hub `semantic_view/validators/sql_ast.py`와 동일 계약이다
(서비스 분리를 위해 구현은 독립, negative fixture로 동작 일치를 검증).
"""
from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

DIALECT = "postgres"


class ArtifactGuardError(RuntimeError):
    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("; ".join(violations))


def build_allowlist(artifact_payload: dict[str, Any]) -> dict[str, Any]:
    tables: set[str] = set()
    columns: dict[str, set[str]] = {}
    join_edges: set[frozenset[tuple[str, str]]] = set()
    mandatory_filters: list[str] = []

    def _add_column(ref: dict[str, Any]) -> None:
        table = ref["object_name"].upper()
        tables.add(table)
        if ref.get("column_name"):
            columns.setdefault(table, set()).add(ref["column_name"].upper())

    for binding in artifact_payload.get("bindings", []):
        spec = binding.get("spec") or {}
        if spec.get("base_object"):
            tables.add(spec["base_object"]["object_name"].upper())
        for ref in spec.get("source_columns") or []:
            _add_column(ref)
        for join in spec.get("join_conditions") or []:
            _add_column(join["left"])
            _add_column(join["right"])
            join_edges.add(frozenset({
                (join["left"]["object_name"].upper(),
                 join["left"]["column_name"].upper()),
                (join["right"]["object_name"].upper(),
                 join["right"]["column_name"].upper()),
            }))
        if binding.get("binding_type") == "FILTER" and spec.get("filter_is_mandatory"):
            mandatory_filters.append(spec["expression_sql"])
    return {"tables": tables, "columns": columns,
            "join_edges": join_edges, "mandatory_filters": mandatory_filters}


def _canonical_predicate(node: exp.Expression, alias_map: dict[str, str],
                         default_table: str | None = None) -> str:
    normalized = node.copy()
    for column in normalized.find_all(exp.Column):
        table = (column.table or "").upper()
        if table in alias_map:
            column.set("table", exp.to_identifier(alias_map[table]))
        elif not table and default_table:
            column.set("table", exp.to_identifier(default_table))
    return normalized.sql(dialect=DIALECT).upper().replace('"', "")


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


def _scope_tables(select: exp.Select) -> list[exp.Table]:
    sources: list[exp.Expression] = []
    from_arg = select.args.get("from_") or select.args.get("from")
    if from_arg is not None:
        sources.append(from_arg.this)
    for join in select.args.get("joins") or []:
        sources.append(join.this)
    return [s for s in sources if isinstance(s, exp.Table)]


def _belongs_to(node: exp.Expression, select: exp.Select) -> bool:
    return node.find_ancestor(exp.Select) is select


def validate_sql_against_artifact(
    sql_text: str,
    artifact_payload: dict[str, Any],
    *,
    dialect: str = DIALECT,
) -> list[str]:
    """위반 목록을 반환한다. 비어 있으면 통과."""
    errors: list[str] = []
    allow = build_allowlist(artifact_payload)
    try:
        statements = sqlglot.parse(sql_text, dialect=dialect)
    except sqlglot.errors.ParseError as e:
        return [f"SQL 파싱 실패: {e}"]
    if len(statements) != 1 or statements[0] is None:
        return ["단일 SELECT 문만 허용됩니다."]
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union)) and not tree.find(exp.Select):
        return ["SELECT 문만 허용됩니다."]
    for node in tree.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Drop,
                             exp.Create, exp.Alter, exp.Command)):
            return [f"허용되지 않은 구문: {type(node).__name__}"]

    cte_names = {c.alias.upper() for c in tree.find_all(exp.CTE)}

    for table in tree.find_all(exp.Table):
        name = table.name.upper()
        if name in cte_names:
            continue
        if name not in allow["tables"]:
            errors.append(f"허용되지 않은 테이블: {name}")

    for select in tree.find_all(exp.Select):
        alias_map: dict[str, str] = {}
        for table in _scope_tables(select):
            name = table.name.upper()
            if name in cte_names:
                continue
            alias_map[(table.alias or table.name).upper()] = name
        scope_tables = set(alias_map.values())
        select_aliases = {
            e.alias.upper() for e in select.expressions if isinstance(e, exp.Alias)
        }
        for column in select.find_all(exp.Column):
            if not _belongs_to(column, select):
                continue
            qualifier = (column.table or "").upper()
            name = column.name.upper()
            if qualifier in cte_names:
                continue
            if qualifier:
                resolved = alias_map.get(qualifier, qualifier)
                if resolved in cte_names or resolved not in allow["tables"]:
                    continue
                if name != "*" and name not in allow["columns"].get(resolved, set()):
                    errors.append(f"허용되지 않은 컬럼: {resolved}.{name}")
            else:
                if name == "*" or name in select_aliases:
                    continue
                if len(scope_tables) == 1:
                    only = next(iter(scope_tables))
                    if name not in allow["columns"].get(only, set()):
                        errors.append(f"허용되지 않은 컬럼: {only}.{name}")
                elif scope_tables and not any(
                        name in cols for cols in allow["columns"].values()):
                    errors.append(f"허용되지 않은 컬럼(비한정): {name}")

        for eq in select.find_all(exp.EQ):
            if not _belongs_to(eq, select):
                continue
            left_column = eq.left if isinstance(eq.left, exp.Column) else None
            right_column = eq.right if isinstance(eq.right, exp.Column) else None
            if left_column is None or right_column is None:
                continue
            lt = alias_map.get((left_column.table or "").upper())
            rt = alias_map.get((right_column.table or "").upper())
            if not lt or not rt or lt == rt:
                continue
            edge = frozenset({(lt, left_column.name.upper()),
                              (rt, right_column.name.upper())})
            if edge not in allow["join_edges"]:
                errors.append(
                    f"허용되지 않은 join: {lt}.{left_column.name.upper()} = "
                    f"{rt}.{right_column.name.upper()}")

        if not scope_tables:
            continue
        default_table = next(iter(scope_tables)) if len(scope_tables) == 1 else None
        where = select.args.get("where")
        canonical_conjuncts = {
            _canonical_predicate(c, alias_map, default_table)
            for c in _conjuncts(where.this if where else None)
        }
        for mandatory in allow["mandatory_filters"]:
            mandatory_tree = sqlglot.parse_one(mandatory, dialect=dialect)
            mandatory_tables = {
                (c.table or "").upper().replace('"', "")
                for c in mandatory_tree.find_all(exp.Column)
            }
            if not (mandatory_tables & scope_tables):
                continue
            canonical_mandatory = _canonical_predicate(mandatory_tree, {})
            if canonical_mandatory not in canonical_conjuncts:
                errors.append(
                    f"mandatory filter 누락 또는 우회: {canonical_mandatory}")
    return errors


def enforce(sql_text: str, artifact_payload: dict[str, Any],
            *, dialect: str = DIALECT) -> None:
    violations = validate_sql_against_artifact(
        sql_text, artifact_payload, dialect=dialect)
    if violations:
        raise ArtifactGuardError(violations)
