"""gen-1 (K-AIR-meta-api 8096) vs gen-2 (robo-meta-api 8097) 응답 동등성 비교.

PRD VER-MN-03 게이트: 동일 질의 50건, top-K Jaccard ≥ 0.95.

본 스크립트는 (a) 표면 응답 키 호환성 + (b) 후보 (schema, table) 집합 Jaccard 두 축을 측정.
matched_columns 까지 비교는 후속 작업 (자산 ①의 컬럼 매칭과 gen-1 의 t2s_columns 매칭이
백엔드 차이로 인해 score 분포가 달라 직접 비교 의미가 약함).

OPENAI quota 초과 등으로 양쪽 모두 keyword 폴백일 때는 표기.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import requests

GEN1 = "http://127.0.0.1:8096"
GEN2 = "http://127.0.0.1:8097"
OUT = Path(__file__).parent.parent / "logs" / "compare_gen1_gen2"
OUT.mkdir(parents=True, exist_ok=True)

DEFAULT_QUERIES = [
    "RDITAG 태그 마스터 테이블 컬럼",
    "원수 탁도 5분 평균값",
    "지난 한 달간 시간별 유량",
    "정수처리 약품 투입량",
    "한국전력 사용 전력량",
    "RWIS 본부 코드",
    "탁도 알람 발생 이력",
    "센서 설비 코드 매핑",
    "응집제 주입율 일평균",
    "용수 공급량 누적",
]


def _post_decision(base: str, query: str) -> Dict[str, Any]:
    r = requests.post(
        f"{base}/data_decision",
        json={"query": query, "include_matched_columns": False},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def _topk_set(resp: Dict[str, Any], k: int) -> Set[Tuple[str, str]]:
    cands = resp.get("candidates") or []
    return {(str(c.get("schema_name") or "").lower(), str(c.get("table_name") or "").lower()) for c in cands[:k]}


def _jaccard(a: Set, b: Set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def main() -> int:
    queries_file = Path(__file__).parent / "compare_queries.txt"
    if queries_file.exists():
        queries = [q.strip() for q in queries_file.read_text(encoding="utf-8").splitlines() if q.strip() and not q.strip().startswith("#")]
    else:
        queries = DEFAULT_QUERIES

    results: List[Dict[str, Any]] = []
    j_top5 = []
    j_top10 = []
    for i, q in enumerate(queries, 1):
        try:
            r1 = _post_decision(GEN1, q)
        except Exception as exc:
            print(f"[{i}/{len(queries)}] gen-1 FAIL: {exc}")
            continue
        try:
            r2 = _post_decision(GEN2, q)
        except Exception as exc:
            print(f"[{i}/{len(queries)}] gen-2 FAIL: {exc}")
            continue
        s1_5 = _topk_set(r1, 5)
        s2_5 = _topk_set(r2, 5)
        s1_10 = _topk_set(r1, 10)
        s2_10 = _topk_set(r2, 10)
        jt5 = _jaccard(s1_5, s2_5)
        jt10 = _jaccard(s1_10, s2_10)
        j_top5.append(jt5)
        j_top10.append(jt10)
        gen1_mode = (r1.get("threshold_used") or {}).get("mode")
        gen2_mode = (r2.get("threshold_used") or {}).get("mode")
        gen1_fb = (r1.get("threshold_used") or {}).get("fallback")
        gen2_fb = (r2.get("threshold_used") or {}).get("fallback")
        results.append({
            "query": q,
            "gen1_target": r1.get("target"), "gen2_target": r2.get("target"),
            "gen1_top5": sorted(list(s1_5)), "gen2_top5": sorted(list(s2_5)),
            "jaccard_top5": jt5, "jaccard_top10": jt10,
            "gen1_mode": gen1_mode, "gen2_mode": gen2_mode,
            "gen1_fallback": gen1_fb, "gen2_fallback": gen2_fb,
        })
        print(f"[{i}/{len(queries)}] J@5={jt5:.3f} J@10={jt10:.3f} q='{q[:30]}' gen1={gen1_mode}/{gen1_fb} gen2={gen2_mode}/{gen2_fb}")

    avg5 = sum(j_top5) / len(j_top5) if j_top5 else 0.0
    avg10 = sum(j_top10) / len(j_top10) if j_top10 else 0.0
    threshold = 0.95
    summary = {
        "queries_total": len(queries),
        "queries_succeeded": len(results),
        "avg_jaccard_top5": round(avg5, 4),
        "avg_jaccard_top10": round(avg10, 4),
        "threshold": threshold,
        "ver_mn_03_pass": avg10 >= threshold,
        "details": results,
    }
    (OUT / "compare_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"=== compare_gen1_gen2 summary ===")
    print(f"queries: {len(results)}/{len(queries)} succeeded")
    print(f"avg Jaccard @top5  = {avg5:.4f}")
    print(f"avg Jaccard @top10 = {avg10:.4f}")
    print(f"VER-MN-03 (avg @top10 >= {threshold}): {'PASS' if avg10 >= threshold else 'FAIL'}")
    return 0 if avg10 >= threshold else 2


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(main())
