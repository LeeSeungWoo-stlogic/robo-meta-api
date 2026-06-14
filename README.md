# robo-meta-api v4

Neo4j meta-api **v0.7** — A안 entity resolution (`resolved_entities` 1차 해소).

- Docker 포트: **8100** (v3 8099와 병행)
- v3 fork 기반, 8 endpoint path 동일

## v0.7 추가

- `POST /data_decision` 응답: `resolved_entities`, `suggested_probes`, `resolution_status`
- FK 1단: `fkTo` + `FK_TO_COLUMN`
- entity resolution: Neo4j master/code 컬럼 → PG `db_probe`

## Docker 기동

```bash
# 의존: robo-postgres(5434), robo-neo4j(7688)
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
python tests/smoke_v07.py
```
