"""Client SourceName.Schema.Table → MindsDB catalog.table rewrite + pair auth."""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp, parse_one

from .execution_context_resolver import ResolvedExecutionContext
from .sql_guard import GuardError


@dataclass(frozen=True)
class SqlTableRef:
    source_key: str
    schema_name: str | None
    table_name: str


def _ident(name: str, *, quoted: bool = True) -> exp.Identifier:
    return exp.Identifier(this=name, quoted=quoted)


def _table_parts(table: exp.Table) -> tuple[str, str, str]:
    """Return (catalog, schema/db, name) with 2-part promotion."""

    name = str(table.name or "")
    catalog = str(table.catalog or "")
    schema = str(table.db or "")
    if not catalog and schema:
        catalog, schema = schema, ""
    return catalog, schema, name


def extract_sql_table_refs(
    sql: str,
    *,
    parser_dialect: str = "mysql",
) -> list[SqlTableRef]:
    try:
        expression = parse_one(sql, read=parser_dialect)
    except Exception as exc:
        raise GuardError(f"SQL parser validation failed: {exc}") from exc

    cte_names = {
        str(cte.alias_or_name).lower()
        for cte in expression.find_all(exp.CTE)
        if cte.alias_or_name
    }
    refs: list[SqlTableRef] = []
    for table in expression.find_all(exp.Table):
        catalog, schema, name = _table_parts(table)
        if not catalog and not schema and name.lower() in cte_names:
            continue
        if not catalog or not name:
            raise GuardError(
                "실행 테이블은 SourceName.Schema.Table 또는 "
                "SourceName.Table / mindsdb_catalog.Table 형식으로 수식해야 합니다."
            )
        refs.append(
            SqlTableRef(
                source_key=catalog,
                schema_name=schema or None,
                table_name=name,
            )
        )
    return refs


def extract_sql_source_keys(
    sql: str,
    *,
    parser_dialect: str = "mysql",
) -> list[str]:
    """Distinct source keys in SQL order (case-preserving first occurrence)."""

    seen: set[str] = set()
    ordered: list[str] = []
    for ref in extract_sql_table_refs(sql, parser_dialect=parser_dialect):
        key = ref.source_key.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(ref.source_key)
    return ordered


def assert_single_sql_source(
    sql: str,
    *,
    parser_dialect: str = "mysql",
) -> str | None:
    keys = extract_sql_source_keys(sql, parser_dialect=parser_dialect)
    if len(keys) > 1:
        raise GuardError(
            "이종 소스 JOIN/다중 소스 수식은 허용되지 않습니다: "
            + ", ".join(keys)
        )
    return keys[0] if keys else None


def _ref_key(schema: str, table: str) -> tuple[str, str]:
    return schema.lower(), table.lower()


def _allowed_ref_keys(
    execution_context: ResolvedExecutionContext,
) -> set[tuple[str, str]]:
    return {
        _ref_key(schema, table)
        for schema, table in execution_context.allowed_object_refs
    }


def _authorize_ref(
    ref: SqlTableRef,
    *,
    execution_context: ResolvedExecutionContext,
) -> None:
    source_name = (execution_context.source_name or "").lower()
    mindsdb = execution_context.catalog.lower()
    key = ref.source_key.lower()
    allowed_refs = _allowed_ref_keys(execution_context)
    allowed_objects = {item.lower() for item in execution_context.allowed_objects}
    distinct_schemas = set(execution_context.allowed_schemas)

    if ref.schema_name:
        # 3-part Source.Schema.Table
        if key not in {source_name, mindsdb} or not key:
            raise GuardError(f"허용되지 않은 catalog/source: {ref.source_key}")
        schema_l = ref.schema_name.lower()
        if schema_l not in distinct_schemas:
            raise GuardError(f"허용되지 않은 schema: {ref.schema_name}")
        if _ref_key(ref.schema_name, ref.table_name) not in allowed_refs:
            raise GuardError(
                f"허용되지 않은 table: {ref.schema_name}.{ref.table_name}"
            )
        return

    # 2-part
    if key == mindsdb:
        if ref.table_name.lower() not in allowed_objects:
            raise GuardError(f"허용되지 않은 table: {ref.table_name}")
        return

    if source_name and key == source_name:
        if len(distinct_schemas) != 1:
            raise GuardError(
                "복수 스키마 소스에서는 SourceName.Schema.Table 3단 수식이 필요합니다"
            )
        only_schema = next(iter(distinct_schemas))
        if _ref_key(only_schema, ref.table_name) not in allowed_refs:
            raise GuardError(f"허용되지 않은 table: {ref.table_name}")
        return

    raise GuardError(f"허용되지 않은 catalog/source: {ref.source_key}")


def qualify_and_rewrite(
    sql: str,
    *,
    execution_context: ResolvedExecutionContext,
) -> str:
    """Authorize (schema, table) pairs, then strip to mindsdb_catalog.table."""

    parser_dialect = execution_context.parser_dialect
    try:
        expression = parse_one(sql, read=parser_dialect)
    except Exception as exc:
        raise GuardError(f"SQL parser validation failed: {exc}") from exc

    cte_names = {
        str(cte.alias_or_name).lower()
        for cte in expression.find_all(exp.CTE)
        if cte.alias_or_name
    }
    require_upper = execution_context.require_quoted_uppercase_identifiers
    mindsdb_catalog = execution_context.catalog

    for table in list(expression.find_all(exp.Table)):
        catalog, schema, name = _table_parts(table)
        if not catalog and not schema and name.lower() in cte_names:
            continue
        if not catalog or not name:
            raise GuardError(
                "실행 테이블은 SourceName.Schema.Table 또는 "
                "SourceName.Table / mindsdb_catalog.Table 형식으로 수식해야 합니다."
            )
        ref = SqlTableRef(
            source_key=catalog,
            schema_name=schema or None,
            table_name=name,
        )
        _authorize_ref(ref, execution_context=execution_context)

        table_name = name
        if require_upper:
            identifier = table.this
            quoted = bool(
                isinstance(identifier, exp.Identifier)
                and identifier.args.get("quoted")
            )
            if not quoted or name != name.upper():
                raise GuardError(
                    "Tibero 식별자는 대문자 인용 식별자를 사용해야 합니다."
                )
            table_name = name.upper()

        replacement = exp.Table(
            this=_ident(table_name, quoted=True),
            db=_ident(mindsdb_catalog, quoted=True),
        )
        if table.alias:
            replacement = replacement.as_(table.alias)
        table.replace(replacement)

    return expression.sql(dialect=parser_dialect)
