# robo-meta-api

자연어 질의를 분석해 SQL 생성에 필요한 최소 Metadata Context를 제공하고, 검증된
읽기 전용 SQL을 federation backend로 실행하는 FastAPI 서비스입니다.

Metadata Store의 승인·활성화된 테이블, 컬럼, embedding, FK 및 논리 join hint를
조회하며 API가 SQL을 직접 생성하지는 않습니다.

## `/v1/data_decision`

자연어 질문을 다음 순서로 처리합니다.

```text
question
  → intent·measurement·entity·schema role 분석
  → JOIN·filter 요구사항 분해
  → question·HyDE·role 다축 embedding 검색
  → score-gap pruning
  → 역할별 테이블 후보 선택
  → 승인 join graph 탐색
  → 최소 테이블 집합과 multi-hop join path 계획
  → execution context 반환
```

주요 응답:

- `query_analysis`: 의도, 측정값, entity, schema role, JOIN·filter 요구사항
- `candidates`: 관련 테이블과 선택 근거 및 관련 컬럼
- `query_plan`: 필수 테이블, bridge 테이블, join path, filter 계획
- `join_groups`: SQL 생성 시 사용할 join 조건 후보
- `execution_context`: 실행 backend, dialect, catalog/schema 및 식별자 규칙
- `resolved_entities`: metadata value mapping 또는 probe로 확인된 entity

LLM query analysis가 실패하면 `degraded` 상태, 사유와 `question_vector` fallback을
반환하며 검색 결과를 근거 없이 확장하지 않습니다.

## `/v1/query_execute`

외부 SQL 생성기가 작성한 SQL을 다음 정책으로 검사한 뒤 MindsDB HTTP SQL API에
전달합니다.

- SELECT 계열 읽기 전용 SQL만 허용
- 허용 catalog/schema와 완전 수식 table 검증
- `execution_context` namespace 검증
- timeout, 최대 행 수, 최대 응답 크기 제한
- 실행 결과와 오류의 audit ID 기록

`artifact_id`가 지정된 경우 Semantic View Artifact의 table·column·join edge·
mandatory filter allowlist를 추가 적용합니다.

## API

| Method | Path | 역할 |
|---|---|---|
| `GET` | `/health` | Metadata Store 및 execution backend 설정 확인 |
| `POST` | `/v1/data_decision` | 자연어 질의 분석과 Metadata Context 계획 |
| `POST` | `/v1/query_execute` | 검증된 읽기 전용 SQL 실행 |
| `POST` | `/meta/batch` | metadata batch 조회 |
| `POST` | `/meta/table` | table metadata 조회 |
| `POST` | `/meta/column` | column metadata 조회 |
| `POST` | `/meta/ref`, `/meta/fk` | FK·논리 관계 조회 |
| `POST` | `/v2/data_decision` | published Semantic View Context Bundle 조회 |

`/data_decision`, `/query_execute`, `/query/execute`는 호환 alias로 유지합니다.

## Semantic View v2

`/v2/data_decision`은 질의 시점에 View를 만들지 않고 published Artifact만
조회합니다.

- tenant/role, published 상태, 유효기간, snapshot 호환성 hard filter
- 준비된 Artifact가 없으면 `readiness=blocked`
- embedding provider 장애 시 임의 lexical 결과로 강등하지 않고 503
- `V2_PG_DSN`이 없으면 v2 배선은 비활성화되고 v1에는 영향을 주지 않음
- `V2_JWKS_FILE`, `V2_ISSUER`, `V2_AUDIENCE` 설정 시 JWT 검증 활성화

## 실행 전제

- 승인·활성화된 `t2s_*` metadata를 제공하는 PostgreSQL Metadata Store
- `/v1/query_execute`에서 사용할 MindsDB HTTP SQL API와 source integration
- query analysis와 embedding에 사용할 OpenAI 호환 endpoint

Neo4j 관련 모듈은 이전 호환 경로로 남아 있지만 `/v1/data_decision`의 metadata
backend는 PostgreSQL입니다.

## 설정

Runtime 설정의 기준은 YAML 파일이며 비밀번호와 API Key는 process environment로
주입합니다.

```bash
export ROBO_RUNTIME_SETTINGS_FILE="$PWD/config/runtime-settings.docker.yaml"
export METADATA_PG_PASSWORD='<metadata-store-password>'
export OPENAI_API_KEY='<api-key>'
```

주요 설정 영역:

- `metadata_store`: PostgreSQL Metadata Store
- `embedding`: embedding endpoint, model, dimension
- `decision`: query analysis, HyDE, 다축 검색, score pruning, join path 정책
- `execution`: MindsDB endpoint, integration, catalog/schema, SQL 제한

`decision.analysis_base_url`을 설정하면 query analysis/HyDE용 chat endpoint를
embedding endpoint와 분리할 수 있습니다.

## 실행

```bash
python -m pip install -r requirements.txt
python -m app.main
```

Docker:

```bash
cp .env.example .env
# OPENAI_API_KEY 등 실제 비밀값 설정
docker compose up -d --build
curl http://127.0.0.1:8100/health
```

## 요청 예시

```bash
curl -sS -X POST http://127.0.0.1:8100/v1/data_decision \
  -H 'Content-Type: application/json; charset=utf-8' \
  -d '{
    "query": "사업장 코드와 사업장 이름을 보여줘",
    "include_matched_columns": true
  }'
```

## 테스트

```bash
python -m pytest tests -q
```

PostgreSQL 통합 시험은 테스트용 DSN 환경 변수가 설정된 경우에만 실행됩니다.

## 코드 구조

- `app/services/query_analysis.py`: 구조화된 자연어 질의 분석
- `app/services/decision_postgres.py`: 다축 검색과 decision 응답 조립
- `app/services/decision_planner.py`: 최소 테이블 집합·join path 계획
- `app/services/metadata_repository.py`: Metadata Store 검색
- `app/services/sql_guard.py`: 읽기 전용 SQL·namespace 검증
- `app/services/query_runner_mindsdb.py`: MindsDB 실행 및 결과 제한
- `app/services/artifact_sql_guard.py`: Semantic View allowlist 검증
- `app/runtime_config.py`: runtime YAML 및 secret 참조
- `tests/`: 단위·통합·회귀 시험
