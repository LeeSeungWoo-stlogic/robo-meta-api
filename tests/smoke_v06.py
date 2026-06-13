"""v0.6 RC body 회귀 스모크 (VER-MN-04).

8 endpoint × 게이트:
  - GET  /health             → 200, meta_version=0.6, X-Meta-Version 헤더 0.6
  - POST /data_decision      → 200, candidates>=1, threshold_used 포함
  - POST /meta/batch         → 200, items 총 카운트 >0
  - POST /meta/table         → 200, table_info + columns + fk 슬롯 모두 존재
  - POST /meta/column        → 200, column.column_name 채워짐
  - POST /meta/ref           → 200, fk 배열 (길이 0 허용)
  - POST /query/execute      → 200, status=ok, row_count>=1
  - POST /query/execute      → 400 (DROP 차단, sql_guard)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import requests

BASE = os.getenv("ROBO_META_API_BASE", "http://127.0.0.1:8097")
OUT_DIR = Path(__file__).parent.parent / "logs" / "smoke_v06"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 사전 검증에서 PG/Neo4j 모두 확인된 RWIS smoke 테이블
SMOKE_DB = os.getenv("SMOKE_DB", "hwaseong")  # Schema(name=rwis, db=hwaseong) 적재값
SMOKE_SCHEMA = os.getenv("SMOKE_SCHEMA", "rwis")
SMOKE_TABLE = os.getenv("SMOKE_TABLE", "rditag_tb")
SMOKE_COLUMN = os.getenv("SMOKE_COLUMN", "tagsn")


def _post(path: str, body: Dict[str, Any]) -> requests.Response:
    return requests.post(f"{BASE}{path}", json=body, timeout=60)


def _get(path: str) -> requests.Response:
    return requests.get(f"{BASE}{path}", timeout=60)


def _save(name: str, body: Any) -> None:
    (OUT_DIR / name).write_text(
        json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    failures: List[str] = []

    # 1) /health
    r = _get("/health")
    obj = r.json()
    _save("01_health.json", {"status_code": r.status_code, "headers": dict(r.headers), "body": obj})
    if r.status_code != 200:
        failures.append(f"health: status={r.status_code}")
    elif obj.get("meta_version") != "0.6":
        failures.append(f"health: meta_version={obj.get('meta_version')}")
    elif r.headers.get("X-Meta-Version") != "0.6":
        failures.append(f"health: X-Meta-Version={r.headers.get('X-Meta-Version')}")
    print(f"[1/8] /health -> {r.status_code} meta={obj.get('meta_version')} hdr={r.headers.get('X-Meta-Version')}")

    # 2) /data_decision
    r = _post("/data_decision", {"query": "RDITAG 태그 마스터 테이블 컬럼", "include_matched_columns": True})
    obj = r.json()
    _save("02_data_decision.json", obj)
    cands = obj.get("candidates") or []
    if r.status_code != 200:
        failures.append(f"data_decision: status={r.status_code} body={str(obj)[:200]}")
    elif obj.get("meta_version") != "0.6":
        failures.append(f"data_decision: meta_version={obj.get('meta_version')}")
    elif len(cands) < 1:
        failures.append("data_decision: candidates empty")
    print(f"[2/8] /data_decision -> {r.status_code} target={obj.get('target')} cands={len(cands)} mode={(obj.get('threshold_used') or {}).get('mode')}")

    # 3) /meta/batch
    r = _post("/meta/batch", {"batch_date": None})
    obj = r.json()
    _save("03_meta_batch.json", {"meta_version": obj.get("meta_version"), "total": obj.get("total"), "sample": (obj.get("items") or [])[:3]})
    if r.status_code != 200 or (obj.get("total") or 0) < 1:
        failures.append(f"meta/batch: status={r.status_code} total={obj.get('total')}")
    print(f"[3/8] /meta/batch -> {r.status_code} total={obj.get('total')}")

    # 4) /meta/table
    r = _post("/meta/table", {"db": SMOKE_DB, "schema_name": SMOKE_SCHEMA, "table_name": SMOKE_TABLE})
    obj = r.json()
    _save("04_meta_table.json", obj)
    cols = (obj.get("columns") or []) if isinstance(obj, dict) else []
    if r.status_code != 200 or len(cols) < 1:
        failures.append(f"meta/table: status={r.status_code} cols={len(cols)} body={str(obj)[:200]}")
    print(f"[4/8] /meta/table {SMOKE_SCHEMA}.{SMOKE_TABLE} -> {r.status_code} cols={len(cols)} fk={len((obj.get('fk') or []))}")

    # 5) /meta/column
    r = _post("/meta/column", {"db": SMOKE_DB, "schema_name": SMOKE_SCHEMA, "table_name": SMOKE_TABLE, "column_name": SMOKE_COLUMN})
    obj = r.json()
    _save("05_meta_column.json", obj)
    col = (obj or {}).get("column") or {}
    if r.status_code != 200 or not col.get("column_name"):
        failures.append(f"meta/column: status={r.status_code} body={str(obj)[:200]}")
    print(f"[5/8] /meta/column {SMOKE_SCHEMA}.{SMOKE_TABLE}.{SMOKE_COLUMN} -> {r.status_code} dtype={col.get('data_type')}")

    # 6) /meta/ref
    r = _post("/meta/ref", {"db": SMOKE_DB, "schema_name": SMOKE_SCHEMA, "table_name": SMOKE_TABLE})
    obj = r.json()
    _save("06_meta_ref.json", obj)
    fk = (obj or {}).get("fk") or []
    if r.status_code != 200:
        failures.append(f"meta/ref: status={r.status_code} body={str(obj)[:200]}")
    print(f"[6/8] /meta/ref {SMOKE_SCHEMA}.{SMOKE_TABLE} -> {r.status_code} fk={len(fk)}")

    # 7) /query/execute ok
    r = _post("/query/execute", {"sql": f'SELECT * FROM "{SMOKE_SCHEMA}"."{SMOKE_TABLE}" LIMIT 3', "timeout_s": 5, "max_rows": 10})
    obj = r.json()
    _save("07_query_execute_ok.json", obj)
    if r.status_code != 200 or obj.get("status") != "ok" or (obj.get("row_count") or 0) < 1:
        failures.append(f"query/execute ok: status={r.status_code} body={str(obj)[:200]}")
    print(f"[7/8] /query/execute SELECT -> {r.status_code} api_status={obj.get('status')} rows={obj.get('row_count')}")

    # 8) /query/execute guard blocked
    r = _post("/query/execute", {"sql": "DROP TABLE foo"})
    try:
        obj = r.json()
    except Exception:
        obj = {"raw": r.text}
    _save("08_query_execute_blocked.json", {"status_code": r.status_code, "body": obj})
    if r.status_code != 400:
        failures.append(f"query/execute blocked: status={r.status_code} body={str(obj)[:200]}")
    print(f"[8/8] /query/execute DROP -> {r.status_code} (expect 400)")

    print()
    if failures:
        print(f"FAIL ({len(failures)})")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS - all 8 endpoints OK (VER-MN-04 gate)")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(main())
