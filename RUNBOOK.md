# robo-meta-api RUNBOOK (v0.7)

## 1) 개요
- 서비스: `robo-meta-api v4`
- 기본 포트: `8100`
- 메타 버전: `0.7`
- 차단 게이트(smoke): `/data_decision` 단일 endpoint

## 2) 선행 의존성

| 구성요소 | 기본 포트 | 비고 |
|---|---:|---|
| `robo-neo4j` | `7687` | 메타 그래프 |
| `robo-postgres` | `5432` | 원천 DB + entity probe |
| `robo-meta-api-v4` | `8100` | 본 서비스 |

## 3) 기동

### 3-1. Docker
```bash
cd c:\Users\LSW\Documents\GitHub\robo-meta-api
cp .env.rwis-test.example .env
docker start robo-postgres robo-neo4j
docker compose up -d --build
curl http://127.0.0.1:8100/health
```

### 3-2. 로컬 실행
```bash
cd c:\Users\LSW\Documents\GitHub\robo-meta-api
cp .env.rwis-test.example .env
python -m app.main
```

## 4) API 경로 (v0.7)
- `GET /health`
- `POST /data_decision`
- `POST /meta/batch`
- `POST /meta/table`
- `POST /meta/column`
- `POST /meta/ref` (기본 FK 경로)
- `POST /meta/fk` (`/meta/ref` alias)
- `POST /query` (stub)
- `POST /query/execute` (deprecated 유지)

## 5) smoke (차단 게이트)

### 5-1. 필수 게이트: `/data_decision` 단일
```bash
python tests/smoke_data_decision_only.py
```

통과 기준(최소):
- `/data_decision` 응답 `200`
- `meta_version`, `resolved_entities`, `suggested_probes`, `resolution_status` 필드 존재

### 5-2. 참고(비차단) 점검
- `/meta/*`, `/query/*`는 회귀 참고 용도로만 사용
- 필요 시 기존 스모크:
```bash
python tests/smoke_v06.py
python tests/smoke_v07.py
```

## 6) 트러블슈팅

| 증상 | 점검 포인트 |
|---|---|
| `/data_decision` 500 | `NEO4J_URI/USER/PASSWORD`, Neo4j 컨테이너 상태 |
| `resolution_status=failed` | `PG_*`, `SOURCE_PG_*`, probe 대상 테이블/권한 |
| `keyword_only` 모드 | `OPENAI_API_KEY` 미설정 또는 외부 LLM 경로 오류 |
| 후보가 비정상적으로 적음 | `DECISION_SCHEMA_ALLOWLIST` 확인 |

## 7) 시크릿/로그
- `.env`는 커밋 금지 (`.gitignore`)
- 감사 로그: `logs/query_audit.jsonl`
- 외부 키 변경 시 재기동 필요

## 8) 변경 기록(260623 계획 반영)
- KAIR 단일 SoT 읽기 전략 고정
- `/meta/fk` alias 추가
- `/query` stub 노출
- smoke 차단 게이트를 `/data_decision` 단일로 조정

## 9) `/query/execute` SourceName 운영 (260805)
- 클라이언트 SQL: `` `SourceName`.`Schema`.`Table` `` (`kair_platform_sources.name`)
- 표시명은 DB UNIQUE — **대소문자만 다른 중복 등록 금지** (lower resolve 모호성)
- `/data_decision` public `catalog`/`integration`/`candidates.db` = 동일 SourceName
- `execution_context`는 Optional; sql-only 가능
- YAML `source_bindings` / `default_source_instance_id` 재도입 금지
- 복수 스키마 소스는 반드시 3단 수식; 2단 `Source.Table`은 DISTINCT schema=1일 때만
