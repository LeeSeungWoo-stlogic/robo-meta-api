# 260623 Scope Freeze

## Purpose
- `robo-meta-api` KAIR 연동 개편의 변경 범위를 고정하고, 구현 중 의사결정 흔들림을 방지한다.

## Frozen Decisions
- **Repository scope**: 변경 대상은 `robo-meta-api`만 허용한다.
- **Reference only**: `KAIR`는 계약/동작 참조 시스템이며 직접 수정하지 않는다.
- **Base branch**: `feature/v4-rwis-e2e`.
- **Delivery branch**: `260623`.
- **Graph strategy**: KAIR 메인 Neo4j(Physical Layer SoT) 단일 읽기.
- **Validation gate**: `/data_decision` 단일 smoke만 차단 게이트로 사용한다.
- **Non-blocking checks**: `/meta/*`, `/query/*`는 참조 점검만 수행한다.

## Out of Scope
- `K-AIR-meta-api` 코드 변경
- KAIR 저장소 코드 변경
- 기능/성능 정량 검증 지표 도입(p95, success ratio 등)
- 실데이터 대규모 적재/마이그레이션 작업

## Change-Control Rule
- 위 항목을 변경하려면 사용자 명시 승인 후 문서 갱신 + 작업 재계획을 수행한다.
