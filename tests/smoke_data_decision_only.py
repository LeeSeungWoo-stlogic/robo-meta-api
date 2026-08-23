"""Minimal gate smoke: /data_decision only.

검증 범위(최소):
- HTTP 200
- meta_version / resolved_entities / resolution_status 필드 존재
"""
from __future__ import annotations

import os
import sys
from typing import Any

import requests


BASE = os.getenv("ROBO_META_API_BASE", "http://127.0.0.1:8100")
QUERY = os.getenv("SMOKE_QUERY", "수지정수장 계측값 현황")


def _fail(msg: str, body: Any | None = None) -> int:
    print(f"FAIL - {msg}")
    if body is not None:
        print(str(body)[:500])
    return 1


def main() -> int:
    try:
        r = requests.post(
            f"{BASE}/data_decision",
            json={
                "query": QUERY,
                "auto_resolve_entities": True,
            },
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(f"/data_decision request error: {type(exc).__name__}: {exc}")

    if r.status_code != 200:
        try:
            body = r.json()
        except Exception:
            body = r.text
        return _fail(f"/data_decision status={r.status_code}", body)

    try:
        body = r.json()
    except Exception as exc:  # noqa: BLE001
        return _fail(f"/data_decision invalid json: {type(exc).__name__}: {exc}", r.text)

    required = ("meta_version", "resolved_entities", "resolution_status")
    missing = [k for k in required if k not in body]
    if missing:
        return _fail(f"missing fields: {', '.join(missing)}", body)

    print(
        "PASS - /data_decision only "
        f"(meta={body.get('meta_version')}, status={body.get('resolution_status')})"
    )
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(main())
