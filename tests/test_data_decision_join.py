""" /data_decision join_groups 전용 검증 (v3 3단 fallback).

Usage:
  python tests/test_data_decision_join.py
  ROBO_META_API_BASE=http://127.0.0.1:8098 python tests/test_data_decision_join.py  # v2 compare
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import requests

BASE = os.getenv("ROBO_META_API_BASE", "http://127.0.0.1:8099")
OUT_DIR = Path(__file__).parent.parent / "logs" / "data_decision_test"
OUT_DIR.mkdir(parents=True, exist_ok=True)

QUERIES = [
    ("plan_primary", "유역본부 내 정수장별 공급량은?"),
    ("supply_by_plant", "정수장별 일일 공급량 조회"),
    ("tag_master", "RDITAG 태그 마스터 테이블"),
    ("scada", "SCADA 센서 측정값과 태그 정보"),
    ("facility", "시설물 코드와 정수장 매핑"),
    ("cross_table", "유역본부 코드와 정수장 코드 조인"),
]


def _post(query: str) -> Dict[str, Any]:
    r = requests.post(
        f"{BASE}/data_decision",
        json={"query": query, "include_matched_columns": True},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def _summarize(name: str, body: Dict[str, Any]) -> Dict[str, Any]:
    cands = body.get("candidates") or []
    jgs = body.get("join_groups") or []
    th = body.get("threshold_used") or {}
    bridges: List[Dict[str, Any]] = []
    for jg in jgs:
        for b in jg.get("bridges") or []:
            bridges.append({
                "from": b.get("from"),
                "to": b.get("to"),
                "via": b.get("via"),
                "confidence": b.get("confidence"),
                "path": b.get("path"),
            })
    return {
        "query_name": name,
        "target": body.get("target"),
        "confidence": body.get("confidence"),
        "candidates": len(cands),
        "candidate_tables": [
            f"{c.get('schema_name')}.{c.get('table_name')}" for c in cands[:5]
        ],
        "join_groups_count": len(jgs),
        "join_groups_mode": th.get("join_groups_mode"),
        "bridges_count": len(bridges),
        "bridges": bridges,
        "via_breakdown": {
            v: sum(1 for b in bridges if b.get("via") == v)
            for v in ("fk", "ontology", "convention")
        },
        "cast_recommended_count": sum(
            1 for b in bridges
            if any(str(p) == "cast_recommended:true" for p in (b.get("path") or []))
        ),
    }


def main() -> int:
    print(f"BASE={BASE}")
    print("=" * 72)
    results: List[Dict[str, Any]] = []
    failures: List[str] = []

    for name, query in QUERIES:
        try:
            body = _post(query)
            summary = _summarize(name, body)
            summary["query"] = query
            results.append(summary)
            _save = OUT_DIR / f"{name}.json"
            _save.write_text(
                json.dumps({"summary": summary, "full": body}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            mode = summary["join_groups_mode"]
            jgc = summary["join_groups_count"]
            bc = summary["bridges_count"]
            via = summary["via_breakdown"]
            cast_n = summary["cast_recommended_count"]
            print(f"[{name}]")
            print(f"  query: {query}")
            print(f"  target={summary['target']} cands={summary['candidates']} "
                  f"join_groups={jgc} mode={mode} bridges={bc}")
            print(f"  via: fk={via['fk']} ontology={via['ontology']} "
                  f"convention={via['convention']} cast_recommended={cast_n}")
            if bc > 0:
                for b in summary["bridges"][:3]:
                    print(f"    - {b['from']} -[{b['via']}]-> {b['to']} "
                          f"conf={b['confidence']} path={b['path']}")
                if bc > 3:
                    print(f"    ... +{bc - 3} more")
            else:
                print("    (no bridges)")
            print()

            if "join_groups_mode" not in (body.get("threshold_used") or {}):
                failures.append(f"{name}: join_groups_mode missing")
            for b in summary["bridges"]:
                if b["via"] == "convention":
                    path = b.get("path") or []
                    if not any(str(p).startswith("shared_column:") for p in path):
                        failures.append(f"{name}: convention missing shared_column path")
                    if not any(str(p).startswith("dtype:") for p in path):
                        failures.append(f"{name}: convention missing dtype path")
        except Exception as e:
            failures.append(f"{name}: {e}")
            print(f"[{name}] ERROR: {e}\n")

    report_path = OUT_DIR / "report.json"
    report_path.write_text(
        json.dumps({"base": BASE, "results": results, "failures": failures},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=" * 72)
    print(f"Saved: {OUT_DIR}")
    total_jg = sum(r["join_groups_count"] for r in results)
    total_br = sum(r["bridges_count"] for r in results)
    nonempty = sum(1 for r in results if r["join_groups_count"] > 0)
    print(f"Summary: {len(results)} queries, {nonempty} with join_groups, "
          f"total groups={total_jg}, total bridges={total_br}")

    if failures:
        print(f"FAIL ({len(failures)})")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(main())
