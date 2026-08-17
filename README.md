# robo-meta-api 1.0

자연어 질의를 분석해 SQL 생성에 필요한 최소 Metadata Context를 제공하고, 검증된
읽기 전용 SQL을 federation backend로 실행하는 FastAPI 서비스입니다.

서비스 버전 **1.0.0**. Metadata Store(K-AIR-metadata-platform)의 승인·활성화된 테이블, 컬럼, embedding,
FK 및 논리 join hint를 조회합니다. `POST /data_decision`은 SQL을 생성하지 않고,
`POST /t2sql`이 확정 SQL과 used 메타를 반환합니다. `/data_decision` meta_version은 **1.0**입니다.

**Serving MVP 소비면:** `POST /data_decision` · `POST /t2sql` · `POST /query_execute`  
(`/data_decision` 1.0. `/t2sql`은 동일 Serving 6표 추가 READ)  
플랫폼 스키마 slim·경계: K-AIR-metadata-platform `docs/ADR-002-SERVING-MVP-AND-SCHEMA-SLIM.md`  
(Wave 0–3 적용 완료 기준, 2026-08-06)

## 1.0 엔진 업데이트 (2026-08-17)

Store 승인 메타만으로 decide/T2SQL을 조립한다. 질문 ID·물리 표명·TAGSN 하드코딩 없음.
라이브 검사 산출(`t2sql_test/`, `_tmp_*`)은 Git 추적에서 제외한다. 단위 테스트 `tests/`는 계약을 잠근다.

| 항목 | 내용 |
| --- | --- |
| **기간·입도** | 기간을 팩트보다 먼저 파싱. ISO 주. 질문 문구 입도 > analyzer hint. 연 창이 day/hour를 월 팩트로 덮지 않음 |
| **시계열 입도** | 월+추이/변화/추세/트렌드 → 일 팩트. 일+동일 시계열 → 시간 팩트. `월별` 등 명시 입도 우선 |
| **time_role** | `latest` / `extremum` / `none`. 목록·추이는 LIMIT 없음. 극값은 측정컬럼 ORDER BY + LIMIT 1 |
| **표 선정** | 카탈로그-only JOIN 드롭. 팩트 [0]/LLM 추측 금지. JOIN은 팩트(없으면 매핑) 앵커만 |
| **별칭·그룹** | 유역/권역/지역본부/유역본부/권역본부. `본부`는 치환 없이 그룹만. `발전` 미사용. `X별`/`X들`은 표 시드 |
| **식별 컬럼** | 차원은 명칭·코드. 설명/비고/광역/사무소 등은 제외 |
| **시계열 출력** | 측정점 키 + 태그 명칭/설명 + 시각 + 원천 VAL. 전일 증감 창작 없음 |
| **측정점 교집합** | 별량 코드매핑 AND 태그 설명에 측정어 |
| **태그 마스터** | 시계열이면 SELECT 대상. 식별은 PK+명칭/설명/별칭. 주소·경로·산출·사이트·상위 코드는 식별에서 제외. JOIN 키는 ON만 |
| **기간 바인딩** | `YYYYMM` LIKE, `YYYYMMDD`/`YYYYMMDDHH` BETWEEN |
| **범위** | `/data_decision` meta_version 1.0. Metadata Store 스키마·값매핑 미변경 |

관련 테스트: `tests/test_engine_contracts.py` · `tests/test_store_first.py` · `tests/test_time_grain.py`

## 이전 작업 요약 (2026-08-11)

| 항목 | 내용 |
| --- | --- |
| **VM 매칭** | `t2s_value_mappings` 공백 무시(`탁 도`↔`탁도`) + Hangul 단독 멘션 경계 보정 |
| **metric→필터** | `measurement.metric`이 Store VM에 매칭될 때만 필수 필터로 승격(표명 하드코딩 없음) |
| **FK 필터 전파** | 해소된 EQ 필터를 승인 `t2s_fk_constraints` **1 hop**으로 plan/bridge 테이블에 복사(코드표→키 표) |
| **역할 보강** | metric이 있으면 태그 마스터 역할 보강 조건 완화 |
| **검증** | `화성정수장 평균 탁도` → `SUJ_CODE=617`·`BR_CODE=TB`·`RDITAG` 전파 후 `/query_execute` AVG 성공 |
| **범위** | RWIS 전용 강바인드 금지. SoT=Store VM·승인 FK. 포맷/unit/facility·산출식은 비범위(플랫폼 값 전파·후속) |

관련 테스트: `tests/test_filter_propagation.py`

## 이전 작업 요약 (2026-08-08)

| 항목 | 내용 |
| --- | --- |
| **모듈 분리** | `decision_postgres` · `metadata_repository` · legacy `decision_service`를 패키지/디렉터리로 분할(임포트 경로 정리, 동작 계약 동일) |
| **KT 피드백 라이브 검증** | Store PUBLISH 후 `/health`·`/data_decision`(충청정수장 탁도…)·`/query_execute`·410 폐기 경로 점검. 응답 슬롯·`resolved_entities`는 **Metadata Store 승인 값매핑·FK 품질**에 의존 (시드/짧은 말 단독 SoT 아님) |
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

**AS-IS (현재 구현):**

```text
question
  → intent·measurement·entity·schema role 분석 (LLM)
  → JOIN·filter 요구사항 분해
  → question·HyDE·role 다축 embedding 검색
  → t2s_value_mappings로 용어→코드 (규칙; 코드 환각 금지)
  → score-gap pruning · 역할별 테이블 후보
  → 승인 t2s_fk_constraints 최단 path
  → join_groups · filters · resolved_entities
  → execution context 반환
```

**목표 제품 순서 (플랫폼 지도 + 본 서비스 조립; ADR-002 §5a-1):**  
resolve(코드) → anchor(키 표) → Fact 선택 → **승인 FK** path → `join_groups`.  
경로 템플릿이 있어도 FK에 없는 JOIN은 채택하지 않는다. LLM은 분해만 하고
코드값·JOIN edge를 확정하지 않는다. 값매핑만으로 전체 해석이 끝난 것이 아니다.

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

- **Hangul mention**: value mapping에서 `정수`⊂`정수장` 같은 중간 글자 오탐을 차단하되, `충청`⊂`충청지역` 등 지명 접미사 복합어는 허용. 라벨 내부 공백(`탁 도`)은 질의 `탁도`와 매칭
- **value mapping 승격**: 매핑된 테이블이 약한 vector score로 gap-prune 되지 않도록 score를 유지·승격
- **차원 질의 랭킹**: 시설/개수·목록 질의에서 `hist`/`link` demote, `master` 우선
- **역할 보강**: HyDE가 사업장+계측을 한 역할로 붕괴할 때 사업장·본부·태그·일별 팩트 역할을 분리(목록형 inventory vs 시계열 구분). `measurement.metric`이 있으면 태그 역할 보강
- **filter**: verified value mapping의 컬럼·코드값을 semantic 컬럼 후보보다 우선. metric은 VM 매칭 시에만 필터 승격. 승인 FK 1 hop으로 plan/bridge에 EQ 필터 전파
- **embedding**: OpenAI `text-embedding-3-*`는 Store와 맞추기 위해 `dimensions`(예: 1024)를 요청하고, GenOS bge-m3 등 matryoshka 미지원 모델에는 해당 필드를 넣지 않음

## `/t2sql`

자연어로 읽기 전용 SQL과 그 SQL에 쓰인 메타(`used_metadata`)를 반환합니다.
성공 SQL은 `/query_execute`에 그대로 넣을 수 있는 SourceName 3단 수식입니다.
`timeout_s`는 파이프라인 벽시계이며 미지정 시 **60초**입니다.
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
| `POST` | `/t2sql` | 자연어 → 확정 SQL + used 메타 (`timeout_s` 미지정 시 60초) |
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
  `timeout_s` 미지정 시 파이프라인 벽시계 기본 60초 (`/query_execute.timeout_s`와 다름).

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
