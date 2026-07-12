# robo-meta-api v4

Neo4j meta-api **v0.7** — A안 entity resolution (`resolved_entities` 1차 해소).

- Docker 포트: **8100** (v3 8099와 병행)
- v3 fork 기반, v0.7 계약 정렬 (`/meta/ref` + `/meta/fk` alias, `/query` stub 노출)

## v0.7 추가

- `POST /data_decision` 응답: `resolved_entities`, `suggested_probes`, `resolution_status`
- FK 1단: `fkTo` + `FK_TO_COLUMN`
- entity resolution: probe registry + PG `db_probe` 기반 1차 해소

## Semantic View v2 추가 (2026-07-13)

외부 T2SQL용 Semantic View Metadata Context Bundle 공급 경로.
기존 v1 `/data_decision` 0.7 계약은 무변경이다 (회귀 시험으로 보증).

- `POST /v2/data_decision` — 질문과 관련된 **published Semantic View Artifact**와
  Metadata Context Bundle(`meta_version: "2"`) 반환. 질의 시점 View 생성 없음.
  - 인증: Bearer JWT (`app/security/auth_context.py` — semantic-hub와 공유
    auth contract). tenant/role은 검증된 token에서만.
  - hard filter: tenant/role·published·유효기간·Snapshot 호환·readiness.
  - ranking: 표준용어/동의어 우선, **vector 유사도 단독 선택 금지**.
  - 적합 Artifact 없으면 `readiness=blocked` + blocker 반환 (fail-closed).
  - provider 장애 시 zero-vector/lexical 강등 없이 503 (fail-closed).
  - 배선: `app.state.v2_deps` (`routers/decision_v2.py`의 `V2Deps`).
    미구성 시 503 — v1 경로 무영향.
- `POST /query/execute` — optional `artifact_id` 추가 (v1 forward-compatible).
  지정 시 기존 read-only guard에 더해 **Artifact allowlist**(허용 table·column·
  join edge·mandatory filter, alias/CTE/subquery/OR 우회 차단)로 검증 후 실행
  (`services/artifact_sql_guard.py`).
- `services/embedding_provider.py` — `_embed_question()` 하드코딩 HTTP 호출을
  provider 인터페이스로 분리 (v1 기본 동작 동일, fixture provider로 결정적 시험).
- Artifact 데이터 원천: 공유 Metadata Store(`t2s_semantic_artifacts` 등,
  semantic-hub가 발행) — `services/v2_store.py`.
- Bundle 응답 계약(SoT): `semantic-hub/semantic_view/schemas/metadata-context-bundle-v2.schema.json`
- 관련 시험: `tests/test_decision_v2.py`, `tests/test_artifact_sql_guard.py`,
  `tests/test_auth_context_contract.py`

## Docker 기동

```bash
# 의존: robo-postgres(5432), robo-neo4j(7687)
docker start robo-postgres robo-neo4j
cp .env.rwis-test.example .env   # OPENAI_API_KEY 입력
docker compose up -d --build
curl http://127.0.0.1:8100/health
```

`docker-compose.yml`은 v4 전용 (`robo-meta-api-v4`, 8100).  
컨테이너 내부 PG/Neo4j는 `host.docker.internal`로 호스트 Docker에 연결.

## RWIS E2E 테스트

```bash
curl -s -X POST http://127.0.0.1:8100/data_decision \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"query":"2025년 9월 15일 수지정수장 계측값 현황 알려줘","auto_resolve_entities":true}'
```

기대: `resolution_status: complete`, `RDISAUP_TB.SUJ_CODE=316` 등 `resolved_entities`.

환경: `.env.rwis-test.example` — `PG_*`(db_probe)와 `SOURCE_PG_*` 동일 값, `DECISION_SCHEMA_ALLOWLIST=rwis` (Request body에 schema 없음).

## 문서

- [`docs/api_spec_v0.7.md`](docs/api_spec_v0.7.md) — **v0.7 API 명세** (v0.6 초안 대비 diff 포함)
- [`docs/design-v4-resolved-entities.md`](docs/design-v4-resolved-entities.md)
- [`docs/external_t2sql_integration.md`](docs/external_t2sql_integration.md)

## smoke

```bash
python tests/smoke_data_decision_only.py
```
