# archive

서비스 기동·Serving 경로(`app/`)에 쓰이지 않는 자료.

여기 있는 파일은 예전 버전 점검·설계 기록이다. `python -m app.main` / Docker 기동에 필요 없다.

| 위치 | 원래 자리 | 왜 옮겼는가 |
|------|-----------|-------------|
| `smoke/` | `tests/smoke_v06.py`, `smoke_v07.py` | 0.6/0.7 시절 라이브 HTTP 회귀. 현재 차단 게이트는 `tests/smoke_data_decision_only.py` |
| `tests/` | semantic-hub 공유 fixture 시험, v2 vs v3 비교 스크립트 | 폐기된 `/semantic_decision`·형제 레포 의존 |
| `docs/` | `api_spec_v0.7.md`, 소비 가이드, entity probe 보고 | 당시 0.7 명세. 현재 계약은 README·OpenAPI |
| `docs-old/` | `docs/old/` | 과거 계획·갭 보고 |
| `scripts/` | `/semantic_decision` probe, gen-1 vs gen-2 비교 | 현재 소비면이 아님 |

현재 엔진 단위 시험(`tests/test_*.py`)과 Serving 스모크(`tests/smoke_data_decision_only.py`)는 `tests/`에 둔다. 런타임 의존은 아니다.
