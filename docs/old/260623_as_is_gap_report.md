# 260623 AS-IS Gap Report

## Scope
- 대상: `robo-meta-api` 코드/문서/테스트 기준의 엔드포인트 및 그래프 소비 계약
- 기준 파일:
  - `app/main.py`
  - `app/routers/meta.py`
  - `app/routers/query_exec.py`
  - `app/schemas.py`
  - `app/services/meta_service.py`
  - `app/services/neo4j_client/vector_search.py`
  - `README.md`
  - `RUNBOOK.md`
  - `docs/api_spec_v0.7.md`

## Gaps (AS-IS)

### G1. `/meta/fk` 명세-구현 불일치
- 문서: `docs/api_spec_v0.7.md`는 `POST /meta/fk`를 정의한다.
- 구현: `app/routers/meta.py`는 `POST /meta/ref`만 제공한다.
- 영향: 외부 소비자가 문서대로 `/meta/fk` 호출 시 404 가능.

### G2. `/query` 스키마-구현 불일치
- 스키마: `app/schemas.py`에 `/query` 스텁 모델(`QueryRequest`, `QueryResponse`)이 존재한다.
- 구현: `app/main.py` + `app/routers/query_exec.py`에서 `/query` 라우트는 미노출.
- 영향: API 계약 이해 혼선, 클라이언트 구현 시 불필요한 분기 발생.

### G3. `HAS_COLUMN` 단일 가정으로 인한 KAIR 호환 저하
- `app/services/meta_service.py` 일부 쿼리:
  - `[:HAS_COLUMN]` 고정
  - `[:fkTo]` 고정
- `app/services/neo4j_client/vector_search.py` 일부 쿼리:
  - `fetch_anchor_columns`에서 `[:HAS_COLUMN]` 고정
- KAIR 기준은 `hasColumn|HAS_COLUMN`, `fkTo|FK_TO_COLUMN` 혼재가 공존한다.
- 영향: KAIR 메인 SoT 읽기 시 컬럼/FK 일부 누락 가능.

### G4. 엔터티 해소 설명-구현 불일치
- README/명세는 “Neo4j master/code 컬럼 + PG db_probe” 뉘앙스를 포함한다.
- 실제 `app/services/entity_resolution.py`는 `driver`를 사용하지 않고 PG probe registry 기반(`del driver`)으로 동작한다.
- 영향: 운영자 관점에서 동작 오해, 장애 시 원인분석 지연.

### G5. RUNBOOK 구버전 정보 잔존
- `RUNBOOK.md`에 포트 `8097`, `meta_version=0.6`, 컨테이너명 `robo-meta-api`가 남아 있다.
- 실제 구현/README는 v4 `8100`, `meta_version=0.7`, `robo-meta-api-v4` 중심이다.
- 영향: 기동/점검 절차 오판 위험.

### G6. README 환경 설명 일부 불일치
- `README.md`는 `robo-postgres(5434)`, `robo-neo4j(7688)` 예시를 안내한다.
- `docker-compose.yml` 기본값은 `host.docker.internal:5432`, `:7687`.
- 영향: 초기 셋업 실패 가능성 증가.

### G7. Smoke Gate 범위와 운영 목적 불일치
- 기존 `tests/smoke_v07.py`는 `smoke_v06.py`를 재사용해 `/meta/*`, `/query/execute`까지 검사한다.
- 현재 운영 목적은 `/data_decision` 동작 여부 단일 게이트.
- 영향: 실데이터 미연결 구간에서 불필요한 게이트 실패 가능.

## Resolution Direction (for implementation)
- `/meta/fk` alias 추가 및 `/meta/ref` 유지
- `/query` 스텁 라우트 명시 노출 또는 문서 제거 중 단일화(본 작업은 노출 채택)
- 그래프 관계명 소비를 KAIR 호환 union 패턴으로 정규화
- entity resolution 설명을 “PG probe registry 기반”으로 명확화
- README/RUNBOOK/API spec을 코드 기준으로 재정렬
- smoke gate를 `/data_decision` 단일 테스트로 분리
