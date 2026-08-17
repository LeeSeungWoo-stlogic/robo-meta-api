"""/semantic_decision Bundle 수신 검증 probe.

폐쇄망 무인증 구성(2026-07-13 결정)이라 토큰 없이
기준 질문의 Metadata Context Bundle(meta_version="2") 수신을 확인한다.
"""
from __future__ import annotations

import json
import sys

import requests

BASE = "http://127.0.0.1:8100"
QUESTION = "2025년 9월 1일 낙동강에서 강우량이 가장 많은 곳은?"


def main() -> int:
    response = requests.post(
        f"{BASE}/semantic_decision",
        json={"query": QUESTION},
        timeout=60,
    )
    print("status:", response.status_code)
    if response.status_code != 200:
        print(response.text[:800])
        return 1
    bundle = response.json()
    summary = {
        "meta_version": bundle.get("meta_version"),
        "readiness": bundle.get("readiness", {}).get("state"),
        "blockers": bundle.get("readiness", {}).get("blockers"),
        "semantic_views": [v.get("view_id") for v in bundle.get("semantic_views", [])],
        "tables": sorted({t["table_name"]
                          for t in bundle.get("schema_context", {}).get("tables", [])}),
        "matched_terms": [m["mention"] for m in
                          bundle.get("query_matches", {}).get("matched_terms", [])],
        "metric_units": [m.get("unit") for m in bundle.get("metric_catalog", [])],
        "evidence": bundle.get("evidence", {}).get("artifacts"),
        "execution_context": bundle.get("execution_context"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    ok = (bundle.get("meta_version") == "2"
          and bundle.get("readiness", {}).get("state") == "ready")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(main())
