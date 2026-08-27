# robo-meta-api 1.0

자연어 질의를 분석해 SQL 생성에 필요한 최소 Metadata Context를 제공하고, 검증된
읽기 전용 SQL을 federation backend로 실행하는 FastAPI 서비스입니다.

서비스 버전 **1.0.0**. Metadata Store(K-AIR-metadata-platform)의 승인·활성화된 테이블, 컬럼, embedding,
FK 및 논리 join hint를 조회합니다. `POST /data_decision`은 SQL을 생성하지 않고,
`POST /t2sql`이 확정 SQL과 used 메타를 반환합니다. `/data_decision` meta_version은 **1.0**입니다.

**Serving MVP 소비면:** `POST /data_decision` · `POST /t2sql` · `POST /query_execute`  
(`/data_decision` 1.0. `/t2sql`은 동일 Serving 6표 추가 READ)  
플랫폼 스키마 slim·경계: K-AIR-metadata-platform `docs/ADR-002-SERVING-MVP-AND-SCHEMA-SLIM.md`  
**업데이트 이력:** [`change_log.md`](change_log.md)

## 서빙 계약

`POST /meta/catalog`과 `POST /data_decision`이 소비면이다. HyDE 벡터 검색용 칸은
`/data_decision` HTTP에 넣지 않는다. `/t2sql`은 용어집 라우트를 내부에서 쓴다.

| 항목 | 내용 |
| --- | --- |
| **`/meta/catalog`** | Serving 정본. 연결·표·컬럼·설명·논리명·`subject_area`·FK. `character varying(n)` → `varchar(n)` |
| **테이블 키** | `source_name` → `db`(원천 database) → `engine` → `schema_name` → `table_name` |
| **후보 표** | 한글 라벨 `table_name_kr`. 상세 `table_description` |
| **후보 컬럼** | `column_description`. 타입은 catalog와 같이 `varchar(n)`. HTTP에 `score` 없음 |
| **분석** | `query_analysis.query`는 요청 원문. 역할은 `schema_roles`. 목표는 `goal` |
| **집계** | `query_plan.aggregation.function` / `tag_combine` / `tags[]`. `tags[]`는 tagsn 바인딩 뒤 `data_process`·`apply`·`unit`(`unit_desc` 표시) |
| **제외 (`/data_decision` HTTP)** | `glossary_routes`, `secondary_targets`, `confidence`, 후보/`matched_columns` `score` |
| **값사전** | `has_code` 유지. 코드 바인딩 SoT는 `query_plan.filters`와 `resolved_entities` |

질문 ID·물리 표명·TAGSN을 코드에 하드코딩하지 않는다. 기간이 필요한 측정 질의는 기간이 없으면 SQL을 만들지 않는다.

## `/data_decision`

자연어 질문을 다음 순서로 처리합니다.

**AS-IS (현재 구현):**

```text
question
  → goal·measurement·schema_roles 분석 (LLM, 물리명 금지)
  → Store 값사전·논리명으로 용어→코드 (규칙; 코드 환각 금지)
  → 팩트 선정 · 승인 필터 바인딩
  → 승인 t2s_fk_constraints 최단 path (뷰 마트는 경로가 비어 있을 수 있음)
  → query_plan.filters · resolved_entities
  → execution context 반환 (source_name, source_instance_id 비공개)
```

**목표 제품 순서 (플랫폼 지도 + 본 서비스 조립; ADR-002 §5a-1):**  
resolve(코드) → anchor(키 표) → Fact 선택 → **승인 FK** path → `join_groups`.  
경로 템플릿이 있어도 FK에 없는 JOIN은 채택하지 않는다. LLM은 분해만 하고
코드값·JOIN edge를 확정하지 않는다. 값매핑만으로 전체 해석이 끝난 것이 아니다.

주요 응답:

- `query_analysis`: 요청 원문, `goal`, 측정값, `schema_roles`, filter 요구사항
- `candidates`: 테이블 키·`table_name_kr`·`table_description`과 관련 컬럼
- `query_plan`: 필수 테이블, bridge, join path, filter, aggregation(`function` / `tag_combine` / `tags[]`)
- `join_groups`: SQL 생성 시 사용할 join 조건 후보
- `execution_context`: 실행 backend, dialect, catalog/schema 및 식별자 규칙
- `resolved_entities`: Store 값사전으로 확인된 entity

LLM 의미 분해가 실패하면 `query_analysis.status=degraded`와 `reason`만 반환한다.
검색 결과를 근거 없이 확장하지 않는다.

### 내용 품질 가드

`/data_decision` 경로·응답 키는 유지하고, 내부 선별·매칭만 보강합니다.

- **한글 기간**: `최근`/`지난`+길이(사흘·일주일·한달 등), 위치(익월·내일·이번주 등). 시계는 KST(`Asia/Seoul`). 길이만 있으면 되묻는다. 표·컬럼명은 쓰지 않는다 (`app/services/decision_postgres/period.py`)
- **Hangul mention**: value mapping에서 `정수`⊂`정수장` 같은 중간 글자 오탐을 차단하되, `충청`⊂`충청지역` 등 지명 접미사 복합어는 허용. 라벨 내부 공백(`탁 도`)은 질의 `탁도`와 매칭
- **value mapping 승격**: 매핑된 테이블이 약한 vector score로 gap-prune 되지 않도록 score를 유지·승격
- **차원 질의 랭킹**: 시설/개수·목록 질의에서 `hist`/`link` demote, `master` 우선
- **역할 보강**: HyDE가 사업장+계측을 한 역할로 붕괴할 때 사업장·본부·태그·일별 팩트 역할을 분리(목록형 inventory vs 시계열 구분). `measurement.metric`이 있으면 태그 역할 보강
- **filter**: verified value mapping의 컬럼·코드값을 semantic 컬럼 후보보다 우선. metric은 VM 매칭 시에만 필터 승격. 승인 FK 1 hop으로 plan/bridge에 EQ 필터 전파
- **embedding**: OpenAI `text-embedding-3-*`는 Store와 맞추기 위해 `dimensions`(예: 1024)를 요청하고, GenOS bge-m3 등 matryoshka 미지원 모델에는 해당 필드를 넣지 않음

## `/t2sql`

자연어로 읽기 전용 SQL과 그 SQL에 쓰인 메타(`used_metadata`)를 반환합니다.
성공 SQL은 `/query_execute`에 그대로 넣을 수 있는 SourceName 3단 수식입니다.
파이프라인 벽시계는 YAML `t2sql.total_timeout_seconds`(기본 **60초**)입니다.
모델은 YAML `t2sql.model` 또는 env `T2SQL_LLM_MODEL`(우선)이며 `app/config.py`에는
T2SQL 키를 넣지 않습니다. `/data_decision` Request/Response 타입을 재사용하지 않습니다.

## `/query_execute`

외부 SQL 생성기가 작성한 SQL을 다음 정책으로 검사한 뒤 MindsDB HTTP SQL API에
전달합니다.

- SELECT 계열 읽기 전용 SQL만 허용
- 허용 catalog/schema와 완전 수식 table 검증
- client `execution_context`를 server-side Metadata Store binding과 재검증
  (`source_instance_id` 또는 SourceName 기반 해석 — YAML `source_bindings` 없음)
- 활성·승인 metadata에서 table allowlist 재계산
- timeout, 최대 행 수, 최대 응답 크기 제한. 요청 `timeout_s` 스키마 상한 600. 기본 60·최댓값 300(`EXEC_DEFAULT_TIMEOUT_S` / `EXEC_MAX_TIMEOUT_S`). HTTP 대기는 `timeout_s + EXEC_ERROR_RETURN_GRACE_S`(기본 30초)로 MindsDB 오류 본문을 `db_error`로 돌려준다
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

## `/meta`

Metadata Store Serving 카탈로그를 질의 없이 조회합니다. 원천 DB를 직접 읽지 않습니다.
`/data_decision`과 같은 Store 행·같은 슬롯 규칙을 쓰되, 점수·계획·엔티티 해소는 없습니다.

| Method | Path | 역할 |
|---|---|---|
| `POST` | `/meta/catalog` | Serving 정본. 연결·표·컬럼·설명·논리명·`subject_area`·FK |
| `POST` | `/meta/batch` | catalog 표 목록을 펼친 요약 |
| `POST` | `/meta/table` | catalog에서 표 하나 |
| `POST` | `/meta/column` | catalog에서 컬럼 하나 |
| `POST` | `/meta/ref` | catalog의 나가는 FK 면 |

정본은 `/meta/catalog` 한 통이다. `/meta/table`·`/meta/column`은 같은 문서를 표·컬럼으로 자른 `CatalogResponse`다. `/meta/batch`와 `/meta/ref`도 catalog에서 파생한다. 별도 `/meta/source`·`/meta/schema`는 없다. 구분 키는 플랫폼 소스(등록 단위)다.

표·컬럼 슬롯:

- `comment`: Store 원본 DDL/카탈로그 설명
- `description`: 검수·서빙 설명. 분석 설명 우선, 없으면 `comment`
- `logical_name`: 승인 한글 논리명. SQL 식별자가 아님. batch의 `table_name_kr`과 같음
- `subject_area`: `/data_decision` 후보와 동일 헬퍼
- 스키마는 소스의 `source_schema`만. 표에 `schema_name`을 반복하지 않음
- 컬럼: `column_name` / `data_type` / `nullable` / `primary_key`
- 나가는 FK: `references` (`constraint_name`·`position` 포함)
- 들어오는 FK: `referenced_by`

`/meta/catalog`는 질의 없이 Store Serving 행만 본다. `source_instance_id`·MindsDB 식별자·시크릿은 없다.
`character varying(n)`은 Store에 길이가 있으면 `varchar(n)`으로 표기한다. 길이가 없으면 창작하지 않는다.

`/meta/ref`는 요청 표에서 **나가는** 관계를 catalog `references`에서 꺼냅니다.
들어오는 관계는 대상 표 컬럼의 `referenced_by`로 본다.

## API

| Method | Path | 역할 |
|---|---|---|
| `GET` | `/health` | Store 소스 목록 조회 가능 여부(`t2sql_configured` 포함). Store 조회 실패 시 503 |
| `POST` | `/data_decision` | 자연어 질의 분석과 Metadata Context 계획 |
| `POST` | `/t2sql` | 자연어 → 확정 SQL + used 메타 (벽시계는 `t2sql.total_timeout_seconds`, 기본 60초) |
| `POST` | `/query_execute` | 검증된 읽기 전용 SQL 실행 |
| `POST` | `/meta/catalog` | Serving 카탈로그 구조 조회 |
| `POST` | `/meta/batch` | metadata batch 조회 |
| `POST` | `/meta/table` | table metadata 조회 |
| `POST` | `/meta/column` | column metadata 조회 |
| `POST` | `/meta/ref` | FK·논리 관계 조회 |

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
`/data_decision`, `/t2sql`, `/query_execute`입니다.

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
- `t2sql`: `/t2sql` LLM 모델·파이프라인 제한. `model` 미설정(및 env `T2SQL_LLM_MODEL`
  공백)이면 부팅은 되고 `POST /t2sql`은 200 + `UPSTREAM_UNAVAILABLE`.
  파이프라인 벽시계는 `t2sql.total_timeout_seconds`(기본 60초). `/query_execute.timeout_s`와 다름.

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
cp config/runtime-settings.example.yaml \
   config/runtime-settings.docker.local.yaml
export ROBO_RUNTIME_SETTINGS_FILE="$PWD/config/runtime-settings.docker.local.yaml"
python -m app.main
```

`ROBO_RUNTIME_SETTINGS_FILE`과 `.env`의 `METADATA_PG_PASSWORD`가 없으면 기동에 실패한다.
상세는 [`RUNBOOK.md`](RUNBOOK.md).

Docker (K-AIR Store가 떠 있고 `kair-metadata-platform_control-plane`이 있어야 함):

```bash
cp .env.example .env
cp config/runtime-settings.example.yaml config/runtime-settings.docker.local.yaml
# METADATA_PG_PASSWORD · OPENAI_API_KEY 등 실제 비밀값 설정
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
분리·audit·semantic 410 stub 검증을 포함한다. 수집 건수는 커밋마다 달라지므로
`python -m pytest tests -q` 결과를 따른다.

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
