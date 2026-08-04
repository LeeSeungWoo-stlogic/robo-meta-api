from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlglot import exp, parse_one

from ..runtime_config import get_runtime
from .artifact_sql_guard import ArtifactGuardError, enforce as enforce_artifact
from .execution_context_resolver import ResolvedExecutionContext
from .sql_guard import GuardError, check


def _serialize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    try:
        return value.isoformat()
    except Exception:
        return str(value)


def _append_audit(entry: dict[str, Any]) -> None:
    runtime = get_runtime()
    path = Path(runtime.execution.audit_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _validate_namespaces(
    sql: str,
    execution_context: ResolvedExecutionContext | None = None,
) -> None:
    if not isinstance(execution_context, ResolvedExecutionContext):
        raise GuardError("ResolvedExecutionContext가 필요합니다")
    parser_dialect = execution_context.parser_dialect
    expected_catalogs = execution_context.allowed_catalogs
    expected_schemas = execution_context.allowed_schemas
    allowed_objects = {
        value.lower() for value in execution_context.allowed_objects
    }
    require_quoted_uppercase = (
        execution_context.require_quoted_uppercase_identifiers
    )
    try:
        expression = parse_one(sql, read=parser_dialect)
    except Exception as exc:
        raise GuardError(f"SQL parser validation failed: {exc}") from exc
    cte_names = {
        str(cte.alias_or_name).lower()
        for cte in expression.find_all(exp.CTE)
        if cte.alias_or_name
    }
    for table in expression.find_all(exp.Table):
        name = str(table.name or "")
        catalog = str(table.catalog or "")
        schema = str(table.db or "")
        if not catalog and not schema and name.lower() in cte_names:
            continue
        # MindsDB integration SQL의 실제 이름 형식은 integration.table이다.
        # sqlglot은 이때 integration을 db로 파싱하므로 catalog로 승격한다.
        if not catalog and schema:
            catalog, schema = schema, ""
        if not catalog:
            raise GuardError(
                "MindsDB 실행 테이블은 integration.table 형식으로 완전 수식해야 합니다."
            )
        if catalog.lower() not in expected_catalogs:
            raise GuardError(f"허용되지 않은 catalog: {catalog}")
        if schema and schema.lower() not in expected_schemas:
            raise GuardError(f"허용되지 않은 schema: {schema}")
        if allowed_objects and name.lower() not in allowed_objects:
            raise GuardError(f"허용되지 않은 table: {name}")
        if require_quoted_uppercase:
            identifier = table.this
            quoted = bool(
                isinstance(identifier, exp.Identifier)
                and identifier.args.get("quoted")
            )
            if not quoted or name != name.upper():
                raise GuardError(
                    "Tibero 식별자는 대문자 인용 식별자를 사용해야 합니다."
                )


async def execute(
    sql: str,
    *,
    timeout_s: int | None,
    max_rows: int | None,
    caller: str | None = None,
    artifact_payload: dict[str, Any] | None = None,
    execution_context: ResolvedExecutionContext,
) -> dict[str, Any]:
    runtime = get_runtime()
    audit_id = str(uuid4())
    try:
        if not isinstance(execution_context, ResolvedExecutionContext):
            raise GuardError("server-side resolved execution context가 필요합니다")
        report = check(sql)
        _validate_namespaces(report.normalized_sql, execution_context)
        if artifact_payload is not None:
            # Semantic View 경로: Artifact allowlist(object/column/join/
            # mandatory filter)를 read-only guard와 결합해 추가 검증
            try:
                enforce_artifact(
                    report.normalized_sql,
                    artifact_payload,
                    dialect=execution_context.parser_dialect,
                )
            except ArtifactGuardError as artifact_exc:
                raise GuardError(
                    f"Artifact allowlist 위반: {artifact_exc}") from artifact_exc
    except GuardError as exc:
        _append_audit(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "audit_id": audit_id,
                "status": "rejected",
                "reason": str(exc),
                "caller": caller,
                "sql": sql,
                "integration": execution_context.integration,
                "resolved_integration": execution_context.integration,
                "execution_context": execution_context.audit_dict(),
            }
        )
        raise

    applied_timeout = int(
        timeout_s
        if timeout_s is not None
        else runtime.execution.default_timeout_seconds
    )
    applied_timeout = min(
        max(applied_timeout, 1),
        runtime.execution.maximum_timeout_seconds,
    )
    applied_rows = int(
        max_rows
        if max_rows is not None
        else runtime.execution.default_max_rows
    )
    applied_rows = min(max(applied_rows, 1), runtime.execution.maximum_rows)
    started = time.perf_counter()
    status = "ok"
    error: str | None = None
    columns: list[str] = []
    rows: list[list[Any]] = []
    truncated = False
    total_bytes = 0

    try:
        async with httpx.AsyncClient(timeout=applied_timeout) as client:
            response = await client.post(
                runtime.execution.sql_api_url,
                json={"query": report.normalized_sql},
            )
            response.raise_for_status()
            body = response.json()
        if body.get("type") != "table":
            status = "db_error"
            error = str(
                body.get("error_message")
                or body.get("error")
                or f"Unexpected MindsDB response type: {body.get('type')}"
            )
        else:
            columns = [str(item) for item in body.get("column_names") or []]
            for raw_row in body.get("data") or []:
                serialized = [_serialize(value) for value in raw_row]
                row_bytes = len(
                    json.dumps(serialized, ensure_ascii=False).encode("utf-8")
                )
                if (
                    len(rows) >= applied_rows
                    or total_bytes + row_bytes
                    > runtime.execution.maximum_response_bytes
                ):
                    truncated = True
                    break
                rows.append(serialized)
                total_bytes += row_bytes
    except httpx.TimeoutException:
        status = "timeout"
        error = f"MindsDB request timeout: {applied_timeout}s"
    except Exception as exc:
        status = "error"
        error = f"{exc.__class__.__name__}: {exc}"

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    audit = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "audit_id": audit_id,
        "status": status,
        "caller": caller,
        "sql": report.normalized_sql,
        "backend": runtime.execution.backend,
        "integration": execution_context.integration,
        "resolved_integration": execution_context.integration,
        "parser_dialect": execution_context.parser_dialect,
        "execution_context": execution_context.audit_dict(),
        "timeout_s_applied": applied_timeout,
        "max_rows_applied": applied_rows,
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
        "error": error,
    }
    _append_audit(audit)
    return {
        "audit_id": audit_id,
        "status": status,
        "error": error,
        "sql_executed": report.normalized_sql,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
        "timeout_s_applied": applied_timeout,
        "max_rows_applied": applied_rows,
        "datasource": (
            execution_context.source_instance_id
        ),
        "query_id": audit_id,
    }
