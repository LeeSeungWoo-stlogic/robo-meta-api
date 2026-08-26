# robo-meta-api RUNBOOK (1.0)

## 1) 개요
- 서비스: `robo-meta-api 1.0`
- 기본 포트: `8100`
- 서비스 버전: `1.0.0`
- 메타 버전: `1.0` (`/data_decision`)
- 차단 게이트(smoke): `/data_decision` 단일 endpoint
- 메타 SoT: K-AIR-metadata-platform Serving Store (`t2s_*`). Neo4j는 운영 경로에 필요 없음

## 2) 선행 의존성

| 구성요소 | 기본 포트 | 비고 |
|---|---:|---|
| K-AIR Metadata Store (`store`) | compose 내부 `5432` (호스트는 `.env`의 `API_PORT`와 별개) | 승인·활성 Serving. 먼저 기동 |
| K-AIR MindsDB (같은 control-plane) | 호스트 비공개 | `/query_execute` 실행 |
| `robo-meta-api` | `8100` | 본 서비스 |

Docker로 붙일 때는 외부 네트워크 `kair-metadata-platform_control-plane`이 있어야 한다.
`docker start robo-neo4j` / `robo-postgres`만으로는 `/health`·`/data_decision`이 서지 않는다.

## 3) 기동

정본 env는 `.env.example`이다. `.env.rwis-test.example`은 구 Neo4j/원천 probe용이며
현행 compose 필수 키(`ROBO_RUNTIME_SETTINGS_FILE`, `METADATA_PG_PASSWORD`)가 없다.

### 3-1. Docker
```bash
cd c:\Users\LSW\Documents\GitHub\robo-meta-api
cp .env.example .env
cp config/runtime-settings.example.yaml config/runtime-settings.docker.local.yaml
```
`.env`에 `METADATA_PG_PASSWORD`(K-AIR Store 암호와 동일)와 필요 시 `OPENAI_API_KEY`를 넣는다.
`runtime-settings.docker.local.yaml`의 `metadata_store`를 기동 중인 K-AIR Store에 맞춘다.
소스 UUID를 YAML에 넣지 않는다.

K-AIR 스택이 떠 있고 네트워크가 있는지 확인한 뒤:

```bash
docker network inspect kair-metadata-platform_control-plane
docker compose up -d --build
curl http://127.0.0.1:8100/health
```

`runtime-settings.docker.local.yaml`은 gitignore. 정본은 `config/runtime-settings.example.yaml`.
`/t2sql` 모델은 로컬 YAML `t2sql.model` 또는 `.env`의 `T2SQL_LLM_MODEL`.
`V2_PG_DSN`은 사용하지 않는다.

### 3-2. 로컬 실행
```bash
cd c:\Users\LSW\Documents\GitHub\robo-meta-api
cp .env.example .env
cp config/runtime-settings.example.yaml config/runtime-settings.docker.local.yaml
export ROBO_RUNTIME_SETTINGS_FILE="$PWD/config/runtime-settings.docker.local.yaml"
# Windows PowerShell: $env:ROBO_RUNTIME_SETTINGS_FILE = "$PWD\config\runtime-settings.docker.local.yaml"
python -m pip install -r requirements.txt
python -m app.main
```
`.env`의 `METADATA_PG_PASSWORD`와 YAML `metadata_store`가 빠져 있으면 기동에 실패한다.

## 4) API 경로 (1.0)
- `GET /health` (`t2sql_configured` 포함. Store `list_execution_sources` 실패 시 503. LLM/probe는 health에서 호출하지 않음)
- `POST /data_decision`
- `POST /t2sql` (파이프라인 벽시계는 `t2sql.total_timeout_seconds`, 기본 60초. statement timeout과 다름)
- `POST /meta/catalog` (서빙 카탈로그 구조. 증강 메타 없음)
- `POST /meta/batch`
- `POST /meta/table`
- `POST /meta/column`
- `POST /meta/ref` (나가는 FK)
- `POST /query_execute` (MindsDB + Store binding. 원천 `SOURCE_PG_*` 직접 실행 아님)
- 폐기: `POST /semantic_decision`(410), `POST /query/execute`(410), `POST /query`(제거)

구문서의 `8097` / `8099` / `/query/execute`는 현행이 아니다.

## 5) smoke (차단 게이트)

### 5-1. 필수 게이트: `/data_decision` 단일
```bash
python tests/smoke_data_decision_only.py
```

통과 기준(최소):
- `/data_decision` 응답 `200`
- `meta_version`, `resolved_entities`, `resolution_status` 필드 존재

`suggested_probes`는 응답 스키마에 없다. 내부 entity probe 잔존 필드이며 게이트 조건이 아니다.

### 5-2. 참고(비차단)
- `/meta/*` 회귀는 운영 차단 게이트가 아니다.
- 0.6/0.7 라이브 스모크는 `archive/smoke/`에 보관했다. 기동에 필요 없다.

## 6) 트러블슈팅

| 증상 | 점검 포인트 |
|---|---|
| compose가 네트워크를 못 찾음 | `kair-metadata-platform_control-plane` 존재 여부. K-AIR를 먼저 `docker compose up` |
| `/health` 503 또는 기동 실패 | `ROBO_RUNTIME_SETTINGS_FILE`, YAML `metadata_store`, `METADATA_PG_PASSWORD` |
| `/data_decision` 500 | Store 연결·Serving 게시 여부. Neo4j가 아니다 |
| `resolution_status=failed` | 값사전·entity. 원천 probe를 쓰면 `SOURCE_PG_*` 권한 |
| `keyword_only` 모드 | `OPENAI_API_KEY` 미설정 또는 YAML embedding/analysis URL |
| `/query_execute` 실패 | Store `t2s_datasources` binding, MindsDB, SourceName 3단 수식 |

## 7) 시크릿/로그
- `.env`는 커밋 금지 (`.gitignore`)
- 감사 로그: `logs/query_audit.jsonl`
- 외부 키 변경 시 재기동 필요

## 8) 변경 기록(260623 계획 반영)
- KAIR 단일 SoT 읽기 전략 고정
- `/meta/fk` alias 제거. FK는 `/meta/ref`만 사용
- `/meta/catalog` 추가. 서빙 연결·표·컬럼 구조만 반환. 나가는 FK는 `references`
- smoke 차단 게이트를 `/data_decision` 단일로 조정

## 8b) ADR-002 Serving MVP (2026-08-06)
- `/semantic_decision` 410 stub; `V2_PG_DSN` 코드 경로 제거
- `/query` stub 제거; `/query/execute` → `/query_execute` (구경로 410)
- `artifact_id` 지정 시 410 (`SEMANTIC_ARTIFACT_GONE`)
- OpenAPI tag: decision → t2sql → query → meta

## 9) `/query_execute` SourceName 운영 (260805)
- 클라이언트 SQL: `` `SourceName`.`Schema`.`Table` `` (`kair_platform_sources.name`)
- 표시명은 DB UNIQUE — **대소문자만 다른 중복 등록 금지** (lower resolve 모호성)
- `/data_decision` public `catalog`/`integration`/`candidates.db` = 동일 SourceName
- `execution_context`는 Optional; sql-only 가능
- YAML `source_bindings` / `default_source_instance_id` 재도입 금지
- 복수 스키마 소스는 반드시 3단 수식; 2단 `Source.Table`은 DISTINCT schema=1일 때만
