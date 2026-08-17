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


def _column_parts(column: exp.Column) -> tuple[str, str, str, str]:
    """Return (catalog, schema/db, table, name) with 3-part promotion."""

    name = str(column.name or "")
    table = str(column.table or "")
    schema = str(column.db or "")
    catalog = str(column.catalog or "")
    if not catalog and schema:
        catalog, schema = schema, ""
    return catalog, schema, table, name


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


def _canonical_table_name(
    ref: SqlTableRef,
    *,
    execution_context: ResolvedExecutionContext,
) -> str:
    """Map client table casing to Store original_name (case-insensitive)."""

    if ref.schema_name:
        schema_l = ref.schema_name.lower()
        table_l = ref.table_name.lower()
        for schema, table in execution_context.allowed_object_refs:
            if schema.lower() == schema_l and table.lower() == table_l:
                return table

    by_lower = {
        item.lower(): item for item in execution_context.allowed_objects if item
    }
    matched = by_lower.get(ref.table_name.lower())
    if matched:
        return matched
    return ref.table_name


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


def fold_quoted_idents_lower(
    sql: str,
    *,
    parser_dialect: str = "mysql",
    keep_names: set[str] | frozenset[str] | None = None,
) -> str:
    """Lowercase quoted identifiers for Postgres, except keep_names.

    MindsDB forwards backtick-quoted names as case-preserving Postgres
    identifiers. Store/Tibero metadata often has SUJ_NAME while Postgres
    columns are suj_name. Table original_name must stay in keep_names.
    """

    keep = {str(name).lower() for name in (keep_names or ()) if name}
    try:
        expression = parse_one(sql, read=parser_dialect)
    except Exception:
        return sql
    for ident in expression.find_all(exp.Identifier):
        name = str(ident.this or "")
        if not name or name.lower() in keep:
            continue
        ident.set("this", name.lower())
        ident.set("quoted", True)
    return expression.sql(dialect=parser_dialect)


def _next_table_alias(used: set[str]) -> str:
    index = 1
    while True:
        candidate = f"t{index}"
        if candidate.lower() not in used:
            return candidate
        index += 1


def _public_schema_name(
    ref: SqlTableRef,
    *,
    execution_context: ResolvedExecutionContext,
) -> str | None:
    if ref.schema_name:
        schema_l = ref.schema_name.lower()
        table_l = ref.table_name.lower()
        for schema, table in execution_context.allowed_object_refs:
            if schema.lower() == schema_l and table.lower() == table_l:
                return schema
        return ref.schema_name
    if len(execution_context.allowed_schemas) == 1:
        only = next(iter(execution_context.allowed_schemas))
        for schema, table in execution_context.allowed_object_refs:
            if schema.lower() == only and table.lower() == ref.table_name.lower():
                return schema
        return execution_context.schema_name or only
    return ref.schema_name or execution_context.schema_name or None


def compact_public_sql(
    sql: str,
    *,
    execution_context: ResolvedExecutionContext,
) -> str:
    """Public SQL: alias columns, qualify FROM as Source.Table when possible."""

    if not (sql or "").strip():
        return sql
    source_name = (execution_context.source_name or "").strip()
    if not source_name:
        return sql
    parser_dialect = execution_context.parser_dialect
    try:
        expression = parse_one(sql, read=parser_dialect)
    except Exception:
        return sql

    cte_names = {
        str(cte.alias_or_name).lower()
        for cte in expression.find_all(exp.CTE)
        if cte.alias_or_name
    }
    used_aliases = set(cte_names)
    real_tables: list[exp.Table] = []
    for table in expression.find_all(exp.Table):
        catalog, schema, name = _table_parts(table)
        if not catalog and not schema and name.lower() in cte_names:
            continue
        if not name:
            continue
        real_tables.append(table)
        if table.alias:
            used_aliases.add(str(table.alias).lower())

    node_alias: dict[int, str] = {}
    ident_alias: dict[tuple[str, str, str], str] = {}
    name_aliases: dict[str, list[str]] = {}
    for table in real_tables:
        catalog, schema, name = _table_parts(table)
        ident = (catalog.lower(), schema.lower(), name.lower())
        if table.alias:
            alias = str(table.alias)
        else:
            alias = _next_table_alias(used_aliases)
            used_aliases.add(alias.lower())
        node_alias[id(table)] = alias
        ident_alias.setdefault(ident, alias)
        ident_alias.setdefault(("", schema.lower(), name.lower()), alias)
        ident_alias.setdefault((source_name.lower(), schema.lower(), name.lower()), alias)
        ident_alias.setdefault((source_name.lower(), "", name.lower()), alias)
        if catalog:
            ident_alias.setdefault((catalog.lower(), "", name.lower()), alias)
        name_aliases.setdefault(name.lower(), []).append(alias)

    alias_names = {alias.lower() for alias in node_alias.values()}

    def _alias_for_column(column: exp.Column) -> str | None:
        catalog, schema, table, _name = _column_parts(column)
        if table.lower() in alias_names or table.lower() in cte_names:
            return table
        ident = (catalog.lower(), schema.lower(), table.lower())
        if ident in ident_alias:
            return ident_alias[ident]
        if table:
            aliases = name_aliases.get(table.lower()) or []
            if len(aliases) == 1:
                return aliases[0]
        if not table and len(real_tables) == 1:
            return node_alias[id(real_tables[0])]
        return None

    for column in list(expression.find_all(exp.Column)):
        name = str(column.name or "")
        if not name:
            continue
        alias = _alias_for_column(column)
        if not alias:
            continue
        column.replace(
            exp.Column(
                this=_ident(name, quoted=True),
                table=exp.Identifier(this=alias, quoted=False),
            )
        )

    require_upper = execution_context.require_quoted_uppercase_identifiers
    single_schema = len(execution_context.allowed_schemas) == 1
    for table in real_tables:
        catalog, schema, name = _table_parts(table)
        ref = SqlTableRef(
            source_key=catalog or source_name,
            schema_name=schema or None,
            table_name=name,
        )
        table_name = _canonical_table_name(ref, execution_context=execution_context)
        schema_name = _public_schema_name(ref, execution_context=execution_context)
        if require_upper:
            table_name = table_name.upper()
            if schema_name:
                schema_name = schema_name.upper()
        alias = node_alias[id(table)]
        if single_schema or not schema_name:
            replacement = exp.Table(
                this=_ident(table_name, quoted=True),
                db=_ident(source_name, quoted=True),
            )
        else:
            replacement = exp.Table(
                this=_ident(table_name, quoted=True),
                db=_ident(schema_name, quoted=True),
                catalog=_ident(source_name, quoted=True),
            )
        table.replace(replacement.as_(alias))

    return expression.sql(dialect=parser_dialect, pretty=True)


def to_source_name_sql(
    sql: str,
    *,
    execution_context: ResolvedExecutionContext,
) -> str:
    """Public SQL: SourceName.Schema.Table. MindsDB catalog is not exposed."""

    source_name = (execution_context.source_name or "").strip()
    if not source_name:
        return sql
    parser_dialect = execution_context.parser_dialect
    try:
        expression = parse_one(sql, read=parser_dialect)
    except Exception:
        return sql
    cte_names = {
        str(cte.alias_or_name).lower()
        for cte in expression.find_all(exp.CTE)
        if cte.alias_or_name
    }
    require_upper = execution_context.require_quoted_uppercase_identifiers
    only_schema = None
    if len(execution_context.allowed_schemas) == 1:
        only_schema = next(iter(execution_context.allowed_schemas))
    for table in list(expression.find_all(exp.Table)):
        catalog, schema, name = _table_parts(table)
        if not catalog and not schema and name.lower() in cte_names:
            continue
        if not name:
            continue
        ref = SqlTableRef(
            source_key=catalog or source_name,
            schema_name=schema or None,
            table_name=name,
        )
        table_name = _canonical_table_name(ref, execution_context=execution_context)
        schema_name = schema or only_schema
        if require_upper:
            table_name = table_name.upper()
            if schema_name:
                schema_name = schema_name.upper()
        if schema_name:
            replacement = exp.Table(
                this=_ident(table_name, quoted=True),
                db=_ident(schema_name, quoted=True),
                catalog=_ident(source_name, quoted=True),
            )
        else:
            replacement = exp.Table(
                this=_ident(table_name, quoted=True),
                db=_ident(source_name, quoted=True),
            )
        if table.alias:
            replacement = replacement.as_(table.alias)
        table.replace(replacement)
    return expression.sql(dialect=parser_dialect)


def _rewrite_column_quals(
    expression: exp.Expression,
    *,
    execution_context: ResolvedExecutionContext,
    mindsdb_catalog: str,
    aliases: set[str],
    cte_names: set[str],
) -> None:
    """Strip Source.Schema from columns so MindsDB does not push 4-part quals."""

    allowed_lower = {
        item.lower(): item for item in execution_context.allowed_objects if item
    }
    for column in list(expression.find_all(exp.Column)):
        catalog, schema, table, name = _column_parts(column)
        if not name:
            continue
        if table.lower() in aliases or table.lower() in cte_names:
            if catalog or schema:
                column.set("catalog", None)
                column.set("db", None)
            ident = column.this
            if isinstance(ident, exp.Identifier) and ident.this:
                ident.set("this", str(ident.this))
                ident.set("quoted", True)
            continue
        if not table:
            continue
        if catalog or schema:
            ref = SqlTableRef(
                source_key=catalog or mindsdb_catalog,
                schema_name=schema or None,
                table_name=table,
            )
            table_name = _canonical_table_name(
                ref, execution_context=execution_context
            )
        elif table.lower() in allowed_lower:
            table_name = allowed_lower[table.lower()]
        else:
            continue
        column.replace(
            exp.Column(
                this=_ident(name, quoted=True),
                table=_ident(table_name, quoted=True),
                db=_ident(mindsdb_catalog, quoted=True),
            )
        )


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
    aliases = {
        str(table.alias).lower()
        for table in expression.find_all(exp.Table)
        if table.alias
    }

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

        # Client casing is ignored; rewrite always uses Store original_name.
        table_name = _canonical_table_name(ref, execution_context=execution_context)
        if require_upper:
            # Backtick policy (Tibero/Oracle): keep requiring quoted input for now.
            # Case is normalized above — do not reject lowercase client names.
            identifier = table.this
            quoted = bool(
                isinstance(identifier, exp.Identifier)
                and identifier.args.get("quoted")
            )
            if not quoted:
                raise GuardError(
                    "Tibero 식별자는 인용 식별자를 사용해야 합니다."
                )
            if table_name != table_name.upper():
                raise GuardError(
                    "Tibero Store original_name은 대문자여야 합니다: "
                    f"{table_name}"
                )
            table_name = table_name.upper()

        replacement = exp.Table(
            this=_ident(table_name, quoted=True),
            db=_ident(mindsdb_catalog, quoted=True),
        )
        if table.alias:
            replacement = replacement.as_(table.alias)
        table.replace(replacement)

    _rewrite_column_quals(
        expression,
        execution_context=execution_context,
        mindsdb_catalog=mindsdb_catalog,
        aliases=aliases,
        cte_names=cte_names,
    )
    return expression.sql(dialect=parser_dialect)
