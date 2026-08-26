# PLAN 260805 — /query_execute SQL-only + 소스표시명.스키마.테이블

> 제목의 구경로 `/query/execute`는 구현 후 `/query_execute`로 고정됐다. 구경로는 410.

| 항목 | 내용 |
|------|------|
| **환경** | Windows 로컬 `robo-meta-api` (`260720` 계열) |
| **상태** | Rev.4 — **Implemented** (단위테스트 green) |
| **사용자 확정** | 연결정보 = 플랫폼 소스 **표시명/등록명**(예: RWIS, GIOS). `/data_decision`도 **동일 방식**으로 공개. 한 소스·다중 스키마도 같은 SourceName 적용 |

## 0. 하드 요구 (위반 금지)

1. `/query/execute`는 **`sql`만**으로 가능.
2. 클라이언트 주 SQL UX: **SourceName.Schema.Table** (`kair_<uuid>` 강제 금지).
3. `/data_decision` 공개 식별 = execute에 쓰는 식별 (decision→execute 복붙 성립).
4. 한 소스·**복수 스키마**를 SQL 3단으로 구분 가능해야 함.
5. YAML `source_bindings` / `default_source_instance_id` **재도입 금지**.
6. 요청 필드 **삭제 금지** — `execution_context`는 **Optional 유지**.
7. MindsDB binding SoT = Store (`mindsdb_integration` / `mindsdb_catalog`).

## 1. As-Is

- `QueryExecuteRequest.execution_context` 필수 (`app/schemas.py` ~500).
- `query_exec.py` context / `source_instance_id` 없으면 400.
- `_validate_claim`: claimed `catalog`/`integration` ≡ Store **mindsdb_*** 문자열 일치.
- `public_dict()` catalog/integration = mindsdb; `audit_dict`가 `**public_dict()`를 펼침.
- `query_runner_mindsdb`: `` `mindsdb_catalog`.`table` ``, rewrite 없음.
- `execution_source_scope`: allowlist = `original_name`만 (스키마 미포함); `source_schema` 단일 text.
- 표시명: `kair_platform_sources.name` (UNIQUE). `t2s_tables.db`는 connection database/sid 라벨 — **SourceName 아님**.

## 2. To-Be 계약

```text
Client SQL:  `MySource`.`SCHEMA1`.`TABLE`     # SourceName = kair_platform_sources.name
Robo:        resolve name → profile_id, mindsdb_*, schema allow-set
Send MDB:    `kair_<uuid>`.`TABLE`            # Tibero 2단 유지
Decision EC: source_name + catalog/integration 공개값 = source_name
```

| SQL 형태 | 규칙 |
|----------|------|
| 3단 Source.Schema.Table | Schema ∈ 해당 datasource 활성·승인 테이블의 `DISTINCT t2s_tables.schema_name` |
| 2단 Source.Table | `|DISTINCT schema|==1`일 때만; 인가는 그 단일 DISTINCT schema 쌍 (**source_schema fallback 금지**) |
| 2단 kair_catalog.Table | 하위호환 — mindsdb_catalog로 직접 resolve |
| sql-only | SQL에서 소스 결정; 이종 소스 JOIN/다중 소스 수식 → 400 |
| context + SQL | context의 source와 SQL 수식 소스 불일치 → 400 |
| decision 공개 | EC·(가능하면) candidates.db = 플랫폼 표시명; 다중 스키마도 **동일 SourceName** |

### 다중 스키마 (HR4) — Store 컬럼 확장 없이

- **검증 SoT:** 활성·승인 `t2s_tables`의 `(schema_name, original_name)` 쌍 + `DISTINCT schema_name`.
- `ResolvedExecutionContext.allowed_schemas` = 그 **DISTINCT 집합** (`source_schema` 단독 frozenset **금지**).
- `schema_name`(public/claim) = 대표값 `t2s_datasources.source_schema` (표시용); **인가 판정에는 쓰지 않음** — 인가 SoT는 쌍/`allowed_schemas`(DISTINCT)만.
- 2단 `Source.Table`: `|DISTINCT schema_name| == 1`일 때만 허용. 인가 시 그 **유일한 DISTINCT schema**로 `(schema, table)` 쌍 검사.  
  **`source_schema` fallback으로 인가 금지** (오거부/오허용). `|DISTINCT| != 1`이면 2단 거부 → 3단 요구.
- **스키마·동명 테이블 인가는 rewrite로 스키마가 빠지기 전(`sql_source_qualify`)에서 수행.**  
  rewrite 후 `_validate_namespaces`는 **bare `original_name` 집합**만 검사 (mindsdb catalog + table).
- public/claim `allowed_objects`: **bare original_name 리스트 유지** (decision 복붙·구클라이언트). 내부 scope는 쌍 별도 필드(`allowed_object_refs`)로 resolver/qualify만 사용.
- Tibero 전송은 계속 2단; Schema는 인가에만 사용.

### 공개 vs 내부 필드

| 필드 | public_dict / API | audit_dict / execute 내부 |
|------|-------------------|---------------------------|
| source_name | 표시명 | 동일 |
| catalog, integration | **= source_name** | **= mindsdb_catalog / mindsdb_integration** (오염 금지) |
| schema_name | 요청/대표 스키마 | 동일 |
| source_instance_id | 포함 | 포함 |

## 3. Claim 수락 규칙 (`_validate_claim` 필수 변경)

파일: `app/services/execution_context_resolver.py`  
심볼: `_validate_claim`, `public_dict`, `audit_dict`

- claimed `catalog`/`integration`이 **source_name** 또는 **mindsdb_*** 이면 통과 (정규화 후 Store 실값과 매핑).
- claimed `source_name`이 있으면 source_name과 일치 검사.
- 구클라이언트가 mindsdb UUID catalog를 넣어도 통과 (하위호환).
- DoD: decision이 준 public context를 execute에 그대로 넣어도 **400 아님**.

## 4. 구현 WBS (삽입점)

### P0 Store — `app/services/metadata_repository.py`

- `list_execution_sources` / `execution_source_scope`:  
  `LEFT JOIN kair_platform_sources s ON s.source_id = d.source_id`  
  → `s.name AS source_name`.
- `source_id` NULL 또는 name NULL → 해당 소스는 sql-only 이름 resolve **불가** (fail-closed; profile_id 경로만).
- `resolve_by_source_name(name)` (repository 또는 resolver): exact match 우선, 아니면 lower(name)로 후보; 0건/2건+ → ExecutionBindingError.
- `execution_source_scope`:  
  - `allowed_object_refs`: `(schema_name, original_name)[]`  
  - `allowed_schemas`: DISTINCT schema_name  
  - `allowed_objects` (공개용): bare `original_name` unique list  
- `binding_from_store_row` / `ResolvedExecutionContext`: 위 세 집합을 모두 실어 P1 qualify가 쌍으로 인가.

### P1 SQL — 신설 `app/services/sql_source_qualify.py` + `query_runner_mindsdb.py`

**`query_exec` → `execute` 파이프라인 순서 고정:**

1. (optional) claim EC 검증 / dual-accept  
2. SQL에서 수식 소스 **단일** 추출 (이종 소스 → 400); EC·SQL 소스 불일치 → 400  
3. `resolve` (Store / `source_name` / kair catalog)  
4. `sql_guard.check(client_sql)`  
5. **`qualify_and_rewrite`**: `(schema, table)` ∈ `allowed_object_refs` 인가 **후** `` `mindsdb_catalog`.`table` `` 로 스트립  
6. `_validate_namespaces(executable_sql)` — mindsdb catalog + **bare** table allowlist  
7. MindsDB POST `executable_sql`

`source_name` 없을 때 (JOIN 실패·NULL):
- sql-only **이름** resolve 불가 (profile_id / kair_catalog SQL / 명시 `source_instance_id` 경로만).
- decision `execution_context` = **`null` 고정** (mindsdb catalog를 public catalog로 내보내지 않음).  
  → public `catalog`/`integration` := source_name 계약은 **name 있을 때만** 성립. name 없으면 EC 자체를 생략해 decision→execute 복붙 분기 제거.

해석표: 3단 / 2단 source / 2단 kair.  
SourceName SoT = **`kair_platform_sources.name` only**. SQL qualify·resolve에 `candidates[].db` / `t2s_tables.db` **사용 금지**.

응답: `QueryExecuteResponse.sql_executed` = **rewrite 후** SQL. audit: `sql_client` + `sql_executed`.

Tibero `require_quoted_uppercase`: **테이블** 식별자에 적용; SourceName 세그먼트는 Store name 매칭(대소문자 정책 P0) — 강제 UPPER 인용 의무 없음.

### P2 execute — `app/schemas.py`, `app/routers/query_exec.py`

- `execution_context: Optional[ExecutionContext] = Field(default=None, ...)`
- `ExecutionContext`에 `source_name: Optional[str] = None` 추가.
- 필수 raise 제거; None이면 SQL qualify로 source 결정 → `resolve_execution_context(..., source_instance_id=..., allow_sql_source_resolve=True)`.

### P3 decision — `execution_context_resolver.py`, `decision_postgres.py`

- `public_dict` / `audit_dict` 분리 (위 표).
- `schemas.ExecutionContext` + OpenAPI examples를 SourceName SQL로 갱신.
- `decision_postgres`는 `ExecutionContext(**resolved.public_dict())` 유지.
- **`/data_decision` 공개 식별 = execute와 동일 방식 (사용자 확정):**
  - `execution_context.source_name` + public `catalog`/`integration` = 플랫폼 표시명 (RWIS, GIOS…).
  - 한 소스에 스키마가 여러 개여도 **동일 SourceName**; 구분은 `schema_name` / SQL 3단.
  - `candidates[].db`: JOIN으로 `source_name`을 알면 **공개값을 platform name으로 채움** (SQL SourceName과 정렬).  
    `t2s_tables.db`는 repository 필터·내부 행 키로만 유지하고, **SourceName SoT로 쓰지 않음**.  
    name 없으면 해당 candidate의 db는 기존 `t2s_tables.db` 유지하되, 상위 `execution_context`는 **null** (위 fail-closed).

### P4 테스트·문서

- `tests/test_source_bindings.py`: dual-accept claim, public≠audit catalog, FakeRepository source_name.
- 신설 `tests/test_sql_source_qualify.py`: 3→2, 모호명, kair 하위호환, 복수 스키마 3단, 2단 복수스키마 거부.
- `tests/test_part2_postgres.py` 등: validate는 rewrite 후.
- sql-only 라우터 테스트.
- `docs/openapi.json` 스키마 동기화; `RUNBOOK.md` 표시명 유일·case 유일 운영.
- **DB migration: 없음** (기존 `kair_platform_sources.name` 사용).

## 5. 비범위

- provisioner integration 이름을 사람 이름으로 변경.
- 플랫폼 소스명 UI.
- OA 반입 패키징.
- MindsDB로 schema.table 3단 전송 (Tibero handler 전제 변경).

## 6. DoD 체크

- [x] sql-only 200 경로 (단일 소스 수식)
- [x] decision public catalog = source_name → execute claim 400 아님
- [x] decision candidates가 SourceName(표시명)으로 SQL 3단 구성 가능 (다중 스키마 동일 SourceName)
- [x] name 없으면 decision EC = null (mindsdb를 public catalog로 내보내지 않음)
- [x] `kair_uuid` SQL 하위호환
- [x] 복수 schema_name 테이블 존재 시 3단 통과·2단 Source.Table 거부
- [x] YAML bindings 미재도입
- [x] execution_context 필드 존재·Optional
- [x] audit에 mindsdb catalog 유지
- [x] OpenAPI·단위테스트 green

## 7. 검수 이력

| 회차 | 결과 | 반영 |
|------|------|------|
| 1 적대 | Needs edits | HR4 Store 모순, claim WBS, 순서/스키마 |
| 2 Junior | Needs edits | `_validate_claim`/`audit_dict`, db≠name, 파일 삽입점, 테스트 파일명 |
| 3 적대+전달 | Needs edits | allowed_schemas=DISTINCT; 인가=rewrite 전 쌍; bare validate; EC null if no name; query_exec 순서 |
| 4 Pass gate | Needs edits | 2단 인가=DISTINCT 단일 schema만(source_schema fallback 금지); name 없으면 EC null 고정(legacy public 금지); decision candidates.db=platform name 정렬 |
| 5 Pass | **Pass** | Rev.4 확정. Residual Medium: 대표 schema_name 표시 혼동 가능; name null 시 candidates.db orphan; P3 심볼 삽입점 보강은 구현 시 |
| 6 구현 | **Done** | P0–P4 반영. 단위테스트 58 green |
