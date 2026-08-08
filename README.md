# robo-meta-api

자연어 질의를 분석해 SQL 생성에 필요한 최소 Metadata Context를 제공하고, 검증된
읽기 전용 SQL을 federation backend로 실행하는 FastAPI 서비스입니다.

Metadata Store(K-AIR-metadata-platform)의 승인·활성화된 테이블, 컬럼, embedding,
FK 및 논리 join hint를 조회하며 API가 SQL을 직접 생성하지는 않습니다.

**Serving MVP 소비면(동결):** `POST /data_decision` · `POST /query_execute`  
플랫폼 스키마 slim·경계: K-AIR-metadata-platform `docs/ADR-002-SERVING-MVP-AND-SCHEMA-SLIM.md`  
(Wave 0–3 적용 완료 기준, 2026-08-06)

## 최근 작업 요약 (2026-08-08)

| 항목 | 내용 |
| --- | --- |
| **모듈 분리** | `decision_postgres` · `metadata_repository` · legacy `decision_service`를 패키지/디렉터리로 분할(임포트 경로 정리, 동작 계약 동일) |
| **KT 피드백 라이브 검증** | Store PUBLISH 후 `/health`·`/data_decision`(충청정수장 탁도…)·`/query_execute`·410 폐기 경로 점검. 응답 슬롯은 존재하나 unit/facility/format·`'탁도'` exact alias·권역 fact 정렬은 **Metadata Store 적재 품질**에 의존 |
| **범위** | 본 사이클에서 KT 필드를 robo에 하드코딩하지 않음. Alias·측정 메타 전파는 플랫폼 VALUE_MAPPING/PROJECT 후속 |

## 이전 작업 요약 (2026-08-06)

| 항목 | 내용 |
| --- | --- |
| **ADR-002 Wave 2** | `/semantic_decision` → 항상 **410** (`SEMANTIC_DECISION_GONE`). V2_PG_DSN / Semantic View 배선 제거 |
| **artifact 경로** | `/query_execute`에 `artifact_id` 지정 시 **410** (`SEMANTIC_ARTIFACT_GONE`) |
| **경로 정리** | `POST /query/execute` → **`POST /query_execute`**. 구경로 `/query/execute`는 **410**. stub `POST /query` **제거** |
| **OpenAPI** | tag 순서 `decision` → `query` → `meta`; `decision-v2`는 schema 비노출(`include_in_schema=False`) |
| **Store 정합** | Metadata Store platform-only activation·Serving KEEP 6표와 맞춤. `/data_decision` Request 계약 무변경 |
| **스모크** | `tests/smoke_data_decision_only.py` — Serving DoD 차단 게이트 |

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

## `/query_execute`

외부 SQL 생성기가 작성한 SQL을 다음 정책으로 검사한 뒤 MindsDB HTTP SQL API에
전달합니다.

- SELECT 계열 읽기 전용 SQL만 허용
- 허용 catalog/schema와 완전 수식 table 검증
- client `execution_context`를 server-side Metadata Store binding과 재검증
  (`source_instance_id` 또는 SourceName 기반 해석 — YAML `source_bindings` 없음)
- 활성·승인 metadata에서 table allowlist 재계산
- timeout, 최대 행 수, 최대 응답 크기 제한
- resolved integration·parser dialect·실행 결과의 audit 기록

운영 SQL 수식(권장): `` `SourceName`.`Schema`.`Table` ``
(`kair_platform_sources.name` = public catalog/integration 표시명)

### Store-sourced binding (2026-08)

- Runtime YAML에 `source_bindings` / `default_source_instance_id` / integration·catalog
  등 소스 등록 키가 있으면 **로드 실패**
- 요청의 `source_instance_id`(또는 SourceName 해석)로 Store `t2s_datasources`에서 binding 조립
- Store `mindsdb_integration`/`mindsdb_catalog` 비어 있거나 불일치 시 fail-closed
- decision 후보 컬럼에 `value_examples`·`unit`·`facility_code`·`system_code` 등
  projection metadata 노출 보강

상세: [`docs/REPORT_260803_store_sourced_bindings.md`](docs/REPORT_260803_store_sourced_bindings.md)

## API

| Method | Path | 역할 |
|---|---|---|
| `GET` | `/health` | Metadata Store 및 execution backend 설정 확인 |
| `POST` | `/data_decision` | 자연어 질의 분석과 Metadata Context 계획 |
| `POST` | `/query_execute` | 검증된 읽기 전용 SQL 실행 |
| `POST` | `/meta/batch` | metadata batch 조회 |
| `POST` | `/meta/table` | table metadata 조회 |
| `POST` | `/meta/column` | column metadata 조회 |
| `POST` | `/meta/ref`, `/meta/fk` | FK·논리 관계 조회 |

| 폐기 경로 | 응답 |
|---|---|
| `POST /semantic_decision` | **410** `SEMANTIC_DECISION_GONE` |
| `POST /query/execute` | **410** `QUERY_EXECUTE_PATH_GONE` → `/query_execute` 사용 |
| `POST /query` | **제거** (stub 삭제) |
| `/query_execute` + `artifact_id` | **410** `SEMANTIC_ARTIFACT_GONE` |

OpenAPI: [`docs/openapi.json`](docs/openapi.json) · 로컬 Swagger `/docs`

## Semantic View (폐기)

ADR-002 Wave 2부터 `/semantic_decision`은 항상 **410**을 반환합니다.
Metadata Store의 semantic pack DDL도 DROP되었습니다. Serving MVP 소비면은
`/data_decision`과 `/query_execute`입니다.

## 실행 전제

- 승인·활성화된 Serving `t2s_*` metadata를 제공하는 PostgreSQL Metadata Store  
  (KEEP: datasources / tables / columns / fk_constraints / value_mappings /
  snapshot_activations — platform-only activation)
- `/query_execute`에서 사용할 MindsDB HTTP SQL API와 source integration
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
- `execution`: MindsDB endpoint 및 SQL 제한(timeout/rows). **소스 등록 금지**
  (`source_bindings` / `default_source_instance_id` / integration·catalog 키 존재 시
  로드 실패). 소스 정본은 Metadata Store `t2s_datasources.profile_id`.

요청의 `source_instance_id`로 Store에서 binding을 조립합니다. Store의
`mindsdb_integration`/`mindsdb_catalog`가 비어 있거나 서로 다르면 해당 요청은
fail-closed입니다. 클라이언트가 보낸 integration/catalog/schema와 allowed
object는 권한 정보로 신뢰하지 않습니다.

`decision.analysis_base_url`을 설정하면 query analysis/HyDE용 chat endpoint를
embedding endpoint와 분리할 수 있습니다.

로컬 Docker에서 K-AIR-metadata-platform Store와 같은 네트워크로 붙일 때는
`config/runtime-settings.docker.local.yaml`의 `metadata_store`만 Store에 맞추면
됩니다. 소스 UUID를 YAML에 넣지 않습니다. **`V2_PG_DSN`은 사용하지 않습니다.**

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
# 단위·계약
python -m pytest tests -q

# Serving 차단 게이트 (플랫폼 DoD와 동일)
python tests/smoke_data_decision_only.py
```

현재 suite는 Store-sourced binding·YAML 소스 등록 금지·위조 context·dialect
분리·audit·semantic 410 stub 검증을 포함해 **87** tests collected입니다.

## 알려진 이슈·한계

| 항목 | 설명 | 대응/상태 |
| --- | --- | --- |
| **Semantic View** | 제품 폐기 | 410 stub 유지 — `/data_decision` 사용 |
| **구경로 `/query/execute`** | 오타·구문서 호환 | 410 — `/query_execute`로 이전 |
| **`artifact_id`** | Semantic Artifact allowlist 경로 폐기 | 지정 시 410 |
| **YAML 소스 등록** | `source_bindings` 등 재도입 금지 | Store `t2s_datasources`만 |
| **Neo4j** | 코드 잔존, 운영 경로 비사용 | PG Metadata Store 사용 |
| **플랫폼 Store slim** | dead/semantic/physical/canonical DROP | Serving KEEP 6표만 SELECT — 계약 동결 |
| **크로스 소스 질문형 JOIN** | 플랫폼 UI 비담당 | `/data_decision` bridges |

## 코드 구조

- `app/services/query_analysis.py`: 구조화된 자연어 질의 분석
- `app/services/decision_postgres.py`: 다축 검색과 decision 응답 조립
- `app/services/decision_planner.py`: 최소 테이블 집합·join path 계획
- `app/services/metadata_repository.py`: Metadata Store 검색
- `app/services/execution_context_resolver.py`: server-side source binding과 allowlist
- `app/services/sql_guard.py`: 읽기 전용 SQL·namespace 검증
- `app/services/query_runner_mindsdb.py`: MindsDB 실행 및 결과 제한
- `app/routers/decision_v2.py`: `/semantic_decision` 410 stub
- `app/runtime_config.py`: runtime YAML 및 secret 참조
- `tests/`: 단위·통합·회귀 시험
- `docs/openapi.json`: OpenAPI 스냅샷
- `RUNBOOK.md`: 기동·smoke·트러블슈팅
