# robo-meta-api

자연어 질의를 분석해 SQL 생성에 필요한 최소 Metadata Context를 제공하고, 검증된
읽기 전용 SQL을 federation backend로 실행하는 FastAPI 서비스입니다.

Metadata Store의 승인·활성화된 테이블, 컬럼, embedding, FK 및 논리 join hint를
조회하며 API가 SQL을 직접 생성하지는 않습니다.

## `/data_decision`

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

### 내용 품질 가드 (스키마 불변, 2026-07-31)

`/data_decision` 경로·응답 키는 유지하고, 내부 선별·매칭만 보강합니다.

- **Hangul mention**: value mapping에서 `정수`⊂`정수장` 같은 중간 글자 오탐을 차단하되, `충청`⊂`충청지역` 등 지명 접미사 복합어는 허용
- **value mapping 승격**: 매핑된 테이블이 약한 vector score로 gap-prune 되지 않도록 score를 유지·승격
- **차원 질의 랭킹**: 시설/개수·목록 질의에서 `hist`/`link` demote, `master` 우선
- **역할 보강**: HyDE가 사업장+계측을 한 역할로 붕괴할 때 사업장·본부·태그·일별 팩트 역할을 분리(목록형 inventory vs 시계열 구분)
- **filter**: verified value mapping의 컬럼·코드값을 semantic 컬럼 후보보다 우선
- **embedding**: OpenAI `text-embedding-3-*`는 Store와 맞추기 위해 `dimensions`(예: 1024)를 요청하고, GenOS bge-m3 등 matryoshka 미지원 모델에는 해당 필드를 넣지 않음

## `/query/execute`

외부 SQL 생성기가 작성한 SQL을 다음 정책으로 검사한 뒤 MindsDB HTTP SQL API에
전달합니다.

- SELECT 계열 읽기 전용 SQL만 허용
- 허용 catalog/schema와 완전 수식 table 검증
- client `execution_context`를 server-side source binding과 재검증
- 활성·승인 metadata에서 table allowlist 재계산
- timeout, 최대 행 수, 최대 응답 크기 제한
- resolved integration·parser dialect·실행 결과의 audit 기록

`artifact_id`가 지정된 경우 Semantic View Artifact의 table·column·join edge·
mandatory filter allowlist를 추가 적용합니다.

## API

| Method | Path | 역할 |
|---|---|---|
| `GET` | `/health` | Metadata Store 및 execution backend 설정 확인 |
| `POST` | `/data_decision` | 자연어 질의 분석과 Metadata Context 계획 |
| `POST` | `/query/execute` | 검증된 읽기 전용 SQL 실행 |
| `POST` | `/meta/batch` | metadata batch 조회 |
| `POST` | `/meta/table` | table metadata 조회 |
| `POST` | `/meta/column` | column metadata 조회 |
| `POST` | `/meta/ref`, `/meta/fk` | FK·논리 관계 조회 |
| `POST` | `/semantic_decision` | published Semantic View Context Bundle 조회 |

## Semantic View

`/semantic_decision`은 질의 시점에 View를 만들지 않고 published Artifact만
조회합니다.

- tenant/role, published 상태, 유효기간, snapshot 호환성 hard filter
- 준비된 Artifact가 없으면 `readiness=blocked`
- embedding provider 장애 시 임의 lexical 결과로 강등하지 않고 503
- `V2_PG_DSN`이 없으면 Semantic View 배선은 비활성화되고 `/data_decision`에는 영향을 주지 않음
- `V2_JWKS_FILE`, `V2_ISSUER`, `V2_AUDIENCE` 설정 시 JWT 검증 활성화

## 실행 전제

- 승인·활성화된 `t2s_*` metadata를 제공하는 PostgreSQL Metadata Store
- `/query/execute`에서 사용할 MindsDB HTTP SQL API와 source integration
- query analysis와 embedding에 사용할 OpenAI 호환 endpoint

Neo4j 관련 모듈은 이전 호환 경로로 남아 있지만 `/data_decision`의 metadata
backend는 PostgreSQL입니다. 현재 `/data_decision` 운영 경로와 본 저장소의 기본 시험에는
Neo4j 또는 Apache AGE/GraphDB가 필요하지 않습니다.

## 설정

Runtime 설정의 기준은 YAML 파일이며 비밀번호와 API Key는 process environment로
주입합니다.

```bash
cp config/runtime-settings.example.yaml \
   config/runtime-settings.docker.local.yaml
export ROBO_RUNTIME_SETTINGS_FILE="$PWD/config/runtime-settings.docker.local.yaml"
export METADATA_PG_PASSWORD='<metadata-store-password>'
export OPENAI_API_KEY='<api-key>'
```

`*.local.yaml`은 Git 제외 대상입니다.

주요 설정 영역:

- `metadata_store`: PostgreSQL Metadata Store
- `embedding`: embedding endpoint, model, dimension
- `decision`: query analysis, HyDE, 다축 검색, score pruning, join path 정책
- `execution`: MindsDB endpoint, source별 binding 및 SQL 제한

`execution.source_bindings`의 key는 활성 Metadata Store에 존재하는 정확한
`source_instance_id`여야 합니다. 각 binding은 다음 값을 server-side에서
소유합니다.

- MindsDB integration과 catalog
- source schema와 source DB dialect
- MindsDB/sqlglot parser dialect
- qualification/identifier quote 규칙
- 허용 catalog/schema

Direct ingest datasource의 legacy `mindsdb_integration`·`mindsdb_catalog`이
NULL이면 runtime binding을 사용합니다. 값이 존재하면서 binding과 다르면
startup 또는 요청 시 거부합니다. 클라이언트가 보낸 integration/catalog/schema와
allowed object는 권한 정보로 신뢰하지 않습니다.

`decision.analysis_base_url`을 설정하면 query analysis/HyDE용 chat endpoint를
embedding endpoint와 분리할 수 있습니다.

로컬 Docker에서 K-AIR-metadata-platform Store와 같은 네트워크로 붙일 때는
`config/runtime-settings.docker.local.yaml`의 `metadata_store`·`embedding.dimensions`·
`execution.source_bindings`가 게시된 `source_instance_id`와 일치해야 합니다.

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
curl -sS -X POST http://127.0.0.1:8100/data_decision \
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

현재 suite는 source binding·위조 context·dialect 분리·audit 검증을 포함해
`78/78`입니다.

## 코드 구조

- `app/services/query_analysis.py`: 구조화된 자연어 질의 분석
- `app/services/decision_postgres.py`: 다축 검색과 decision 응답 조립
- `app/services/decision_planner.py`: 최소 테이블 집합·join path 계획
- `app/services/metadata_repository.py`: Metadata Store 검색
- `app/services/execution_context_resolver.py`: server-side source binding과 allowlist
- `app/services/sql_guard.py`: 읽기 전용 SQL·namespace 검증
- `app/services/query_runner_mindsdb.py`: MindsDB 실행 및 결과 제한
- `app/services/artifact_sql_guard.py`: Semantic View allowlist 검증
- `app/runtime_config.py`: runtime YAML 및 secret 참조
- `tests/`: 단위·통합·회귀 시험
