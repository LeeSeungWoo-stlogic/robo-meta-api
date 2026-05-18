"""외부 AI가 보낸 SQL을 robo-postgres(rwis DB)에 실행 대행.

흐름:
  1) sql_guard.check(sql) → 금지 시 즉시 400
  2) BEGIN READ ONLY + SET LOCAL statement_timeout + SET LOCAL search_path
  3) 실행 후 최대 max_rows·max_bytes까지만 fetch
  4) 감사 로그(JSONL append), 결과값은 로그에 저장하지 않음 (REPORT v5 §4.2)
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import psycopg
from psycopg.rows import tuple_row

from ..config import settings
from ..db import source_conn
from .sql_guard import GuardError, check


def _serialize(value: Any) -> Any:
    """JSON 직렬화 가능한 형태로 변환 (datetime/decimal/bytes/memoryview 등)."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except Exception:
            return repr(value)
    try:
        return value.isoformat()
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        pass
    return str(value)


def _append_audit(entry: Dict[str, Any]) -> None:
    path = Path(settings.exec_audit_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class ExecuteResult(Dict[str, Any]):
    pass


async def execute(
    sql: str,
    *,
    timeout_s: Optional[int],
    max_rows: Optional[int],
    caller: Optional[str] = None,
) -> Dict[str, Any]:
    """검증 → 실행 → 결과·감사 반환."""
    # ---- 1) Guard ----
    try:
        report = check(sql)
    except GuardError as exc:
        audit = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "audit_id": str(uuid4()),
            "status": "rejected",
            "reason": str(exc),
            "caller": caller,
            "sql": sql,
        }
        _append_audit(audit)
        raise  # 라우터에서 400 처리

    # ---- 2) 정책 적용 값 산출 ----
    t_out = int(timeout_s or settings.exec_default_timeout_s)
    t_out = max(1, min(t_out, settings.exec_max_timeout_s))
    rmax = int(max_rows or settings.exec_default_max_rows)
    rmax = max(1, min(rmax, settings.exec_max_rows_hard))
    bmax = settings.exec_max_bytes

    audit_id = str(uuid4())
    started = time.perf_counter()

    columns: List[str] = []
    rows: List[List[Any]] = []
    truncated = False
    total_bytes = 0
    status = "ok"
    error: Optional[str] = None

    # ---- 3) 실행 (READ ONLY TX + timeout) ----
    try:
        async with source_conn() as conn:
            await conn.set_autocommit(False)
            async with conn.cursor(row_factory=tuple_row) as cur:
                await cur.execute("BEGIN READ ONLY")
                await cur.execute(f"SET LOCAL statement_timeout = '{t_out}s'")
                await cur.execute("SET LOCAL lock_timeout = '2s'")
                # search_path 기본 — "RWIS" 스키마 힌트 (대문자 식별자 보호)
                await cur.execute(
                    f'SET LOCAL search_path = "{settings.source_pg_schema}", public'
                )

                await cur.execute(report.normalized_sql)

                if cur.description:
                    columns = [d.name for d in cur.description]
                    # fetch loop with caps
                    while True:
                        batch = await cur.fetchmany(200)
                        if not batch:
                            break
                        for r in batch:
                            serial = [_serialize(v) for v in r]
                            row_bytes = len(json.dumps(serial, ensure_ascii=False).encode("utf-8"))
                            if (
                                len(rows) >= rmax
                                or total_bytes + row_bytes > bmax
                            ):
                                truncated = True
                                break
                            rows.append(serial)
                            total_bytes += row_bytes
                        if truncated:
                            break
                await cur.execute("COMMIT")
    except psycopg.errors.QueryCanceled as exc:
        status = "timeout"
        error = f"statement_timeout {t_out}s 초과: {exc}"
    except psycopg.Error as exc:
        status = "db_error"
        error = f"{exc.__class__.__name__}: {exc}"
    except Exception as exc:
        status = "error"
        error = str(exc)

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)

    audit_entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "audit_id": audit_id,
        "status": status,
        "caller": caller,
        "sql": report.normalized_sql,
        "leading_keyword": report.leading_keyword,
        "timeout_s_applied": t_out,
        "max_rows_applied": rmax,
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
        "error": error,
    }
    _append_audit(audit_entry)

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
        "timeout_s_applied": t_out,
        "max_rows_applied": rmax,
    }
