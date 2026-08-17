"""/semantic_decision G1 질문 세트 probe.

golden 질문 + SV-RWIS-MEASURE-DAY representative questions로
Bundle 수신·View 선택·readiness를 일괄 확인한다.
폐쇄망 무인증 구성(2026-07-13 결정)이라 토큰 없이 호출한다.
"""
from __future__ import annotations

import json
import sys

import requests

BASE = "http://127.0.0.1:8100"

QUESTIONS = [
    ("golden(회귀)", "2025년 9월 1일 낙동강에서 강우량이 가장 많은 곳은?"),
    ("G1-2", "2025년 8월 한 달간 사업장별 일평균 유량은?"),
    ("G1-3", "지난주 수위가 가장 높았던 사업장 상위 5곳은?"),
    ("G1-4", "2025년 9월 낙동강유역본부 사업장별 일 공급량 합계는?"),
    ("G1-5", "2025년 저수율이 가장 높았던 사업장은?"),
    ("G5-장비", "화성정수장의 장비 개수"),
    ("G5-태그", "낙동강유역본부에 강우량 태그는 몇 개 있는가?"),
    ("G2-유입량", "어제 화성정수장의 시간대별 유입량 추이는?"),
    ("G2-전력", "화성가압장의 어제 시간별 전력 사용량은?"),
    ("G3-15분", "오늘 오전 화성정수장 잔류염소의 15분 단위 값은?"),
    ("G3-1분", "최근 1시간 특정 태그의 1분 원시값 변화는?"),
    ("G4-실시간", "지금 낙동강유역본부에서 수위가 가장 높은 곳은?"),
    ("G4-통신", "현재 통신 이상 태그는 몇 개인가?"),
    ("G5-조직", "한강유역본부 산하 사무소·사업장 구성은?"),
    ("G6-한전", "2025년 8월 화성정수장의 한전 수전 전력량은?"),
    ("G7-연계", "현재 DB 연계 상태가 비정상인 사무소는?"),
    ("G7-지연", "오늘 데이터 수집이 지연된 지역본부는?"),
]


def main() -> int:
    failures = 0
    for label, question in QUESTIONS:
        response = requests.post(
            f"{BASE}/semantic_decision",
            json={"query": question},
            timeout=60,
        )
        if response.status_code != 200:
            print(f"[{label}] HTTP {response.status_code}: {response.text[:300]}")
            failures += 1
            continue
        bundle = response.json()
        readiness = bundle.get("readiness", {})
        views = [v.get("view_id") for v in bundle.get("semantic_views", [])]
        matched = [m["mention"] for m in
                   bundle.get("query_matches", {}).get("matched_terms", [])]
        tables = sorted({t["table_name"] for t in
                         bundle.get("schema_context", {}).get("tables", [])})
        ok = bundle.get("meta_version") == "2" and readiness.get("state") == "ready"
        print(f"[{label}] {'PASS' if ok else 'FAIL'}"
              f" readiness={readiness.get('state')}"
              f" views={views}"
              f" matched={matched}"
              f" tables={tables}")
        if not ok:
            print("  blockers:", json.dumps(readiness.get("blockers"), ensure_ascii=False))
            failures += 1
    print("RESULT:", "ALL PASS" if failures == 0 else f"{failures} FAIL")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(main())
