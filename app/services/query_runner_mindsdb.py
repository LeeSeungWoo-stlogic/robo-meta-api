from __future__ import annotations

import json
import os
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
from .sql_source_qualify import qualify_and_rewrite, to_source_name_sql


def _repair_latin1_utf8(text: str) -> str:
    """Undo UTF-8 bytes that were decoded as Latin-1. Leave real Hangul alone."""

    if not text or not any("\x80" <= ch <= "\xff" for ch in text):
        return text
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    return repaired


def _serialize(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _repair_latin1_utf8(value)
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    try:
        return value.isoformat()
    except Exception:
        return str(value)


_ERROR_RETURN_GRACE_MAX = 120


def _error_return_grace_seconds() -> int:
    """MindsDB가 오류 JSON을 늦게 돌려줄 때 httpx가 먼저 끊지 않게 하는 여유."""

    raw = os.environ.get("EXEC_ERROR_RETURN_GRACE_S", "30")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 30
    return max(0, min(value, _ERROR_RETURN_GRACE_MAX))


def _mindsdb_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _detect_simple_table_count(sql_text: str, parser_dialect: str = "mysql") -> tuple[bool, str | None, str | None]:
    """WHERE, GROUP BY, HAVING, JOIN 없는 단순 SELECT COUNT(...) FROM table [LIMIT ...] 판별"""
    try:
        expr = parse_one(sql_text, read=parser_dialect)
        if not isinstance(expr, exp.Select):
            return False, None, None
        if expr.args.get("where") or expr.args.get("group") or expr.args.get("having") or expr.args.get("joins"):
            return False, None, None
        selects = expr.selects
        if len(selects) != 1:
            return False, None, None
        sel = selects[0]
        count_func = sel.this if isinstance(sel, exp.Alias) else sel
        if not isinstance(count_func, exp.Count):
            return False, None, None
        tables = list(expr.find_all(exp.Table))
        if len(tables) != 1:
            return False, None, None
        table_name = str(tables[0].name or "")
        alias = sel.alias if isinstance(sel, exp.Alias) else "row_count"
        return True, table_name, alias or "row_count"
    except Exception:
        return False, None, None


def _mindsdb_error_text(
    body: dict[str, Any],
    response: httpx.Response | None = None,
) -> str:
    for key in ("error_message", "error", "message"):
        value = body.get(key)
        if value:
            return str(value)
    text = (response.text if response is not None else "").strip()
    if text:
        return text[:2000]
    if response is not None:
        return f"MindsDB HTTP {response.status_code}"
    return "MindsDB error"


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
    """Post-rewrite guard: mindsdb catalog + bare table allowlist only."""

    if not isinstance(execution_context, ResolvedExecutionContext):
        raise GuardError("ResolvedExecutionContext가 필요합니다")
    parser_dialect = execution_context.parser_dialect
    expected_catalogs = execution_context.allowed_catalogs
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
        if schema:
            raise GuardError(
                "rewrite 후 SQL은 mindsdb_catalog.table 2단이어야 합니다"
            )
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
    sql_client = sql
    executable_sql = sql
    try:
        if not isinstance(execution_context, ResolvedExecutionContext):
            raise GuardError("server-side resolved execution context가 필요합니다")
        report = check(sql_client)
        executable_sql = qualify_and_rewrite(
            report.normalized_sql,
            execution_context=execution_context,
        )
        _validate_namespaces(executable_sql, execution_context)
        if artifact_payload is not None:
            # Semantic View 경로: Artifact allowlist(object/column/join/
            # mandatory filter)를 read-only guard와 결합해 추가 검증
            try:
                enforce_artifact(
                    executable_sql,
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
                "sql": sql_client,
                "sql_client": sql_client,
                "sql_executed": executable_sql,
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
    http_timeout = applied_timeout + _error_return_grace_seconds()

    # 💡 Smart Short-Circuit: WHERE/GROUP BY 없는 단일 테이블 단순 COUNT 질의 탐지
    is_simple_count, target_table, count_alias = _detect_simple_table_count(sql_client, parser_dialect=execution_context.parser_dialect)
    if is_simple_count and target_table:
        try:
            from ..db import get_metadata_repository
            repo = get_metadata_repository()
            detail = await repo.fetch_table_detail(
                db=None,
                schema_name=execution_context.schema_name or "rwis_mart",
                table_name=target_table,
            )
            if detail and detail.get("table"):
                meta = detail["table"].get("metadata") or {}
                if isinstance(meta, str):
                    meta = json.loads(meta)
                fast_count = meta.get("row_count") or meta.get("estimated_row_count")
                if fast_count is not None:
                    elapsed = (time.perf_counter() - started) * 1000
                    col_name = count_alias or "row_count"
                    rows_out = [[int(fast_count)]]
                    _append_audit({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "audit_id": audit_id,
                        "status": "ok",
                        "caller": caller,
                        "sql": sql_client,
                        "sql_client": sql_client,
                        "sql_executed": "[METADATA_SHORT_CIRCUIT] " + sql_client,
                        "backend": "metadata_store",
                        "integration": execution_context.integration,
                        "resolved_integration": execution_context.integration,
                        "parser_dialect": execution_context.parser_dialect,
                        "execution_context": execution_context.audit_dict(),
                        "timeout_s_applied": applied_timeout,
                        "max_rows_applied": applied_rows,
                        "row_count": 1,
                        "truncated": False,
                        "elapsed_ms": round(elapsed, 1),
                        "error": None,
                    })
                    return {
                        "audit_id": audit_id,
                        "columns": [col_name],
                        "rows": rows_out,
                        "truncated": False,
                        "status": "ok",
                        "elapsed_ms": elapsed,
                        "sql_executed": "[METADATA_SHORT_CIRCUIT] " + sql_client,
                    }
        except Exception:
            pass  # Fallback to standard execution if metadata lookup fails

    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            response = await client.post(
                runtime.execution.sql_api_url,
                json={"query": executable_sql},
            )
        body = _mindsdb_payload(response)
        if response.status_code >= 400 or body.get("type") != "table":
            status = "db_error"
            error = _mindsdb_error_text(body, response)
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
        "sql": executable_sql,
        "sql_client": sql_client,
        "sql_executed": executable_sql,
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
        "sql_executed": to_source_name_sql(
            sql_client, execution_context=execution_context
        ),
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
