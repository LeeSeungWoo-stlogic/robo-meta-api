# 260623 Release & Rollback Notes

## Release Scope
- KAIR SoT 호환 그래프 소비 어댑터 추가
- `/meta/fk` alias 추가 (`/meta/ref` 유지)
- `/query` stub endpoint 노출
- `/data_decision` 전처리에서 KAIR 필드(subject_area/datasource) 소비 강화
- 문서 동기화(README, API spec, RUNBOOK)
- `/data_decision` 단일 smoke 게이트 추가

## Release Validation (minimal)
- 단위 테스트:
  - `tests/test_decision_prune.py`
  - `tests/test_decision_policy.py`
  - `tests/test_entity_probe_registry.py`
- smoke gate:
  - `tests/smoke_data_decision_only.py`

## Rollback Plan
1. 배포 브랜치에서 이전 커밋으로 되돌린 태그/커밋을 체크아웃한다.
2. API 경로 영향이 있으면 `/meta/fk`, `/query` 호출자를 기존 `/meta/ref`, `/data_decision` 기반으로 되돌린다.
3. RUNBOOK 기준 smoke(`tests/smoke_data_decision_only.py`)를 다시 실행해 기본 동작을 확인한다.

## Operational Notes
- 본 릴리즈의 차단 게이트는 `/data_decision`만 포함한다.
- `/meta/*`, `/query/*`는 비차단 참조 점검 대상으로 유지한다.
