# robo-meta-api v4 API Specification (v0.7)

> **기준 구현:** `robo-meta-api-v4` (Docker 포트 **8100**)  
> **상위 초안:** K-AIR Meta API Specification Draft **v0.6 RC** (`api_spec_draft.md`)  
> **변경 요약:** v0.6 대비 `/data_decision` **요청 +1필드·응답 +3필드**. 나머지 endpoint body는 v0.6과 **동일(하위 호환)**.

---

## v0.6 → v0.7 변경 요약

| 구분 | v0.6 RC (초안) | v0.7 (v4) | 변경 |
|------|----------------|-----------|------|
| `X-Meta-Version` / `meta_version` | `"0.6"` | `"0.7"` | 버전 bump |
| Base URL (Docker) | `8096` (예시) | **8100** | v4 전용 포트 |
| **POST /data_decision Request** | 3필드 | **4필드** | **+1** (`auto_resolve_entities`) |
| **POST /data_decision Response** | 7 top-level | **10 top-level** | **+3** (아래) |
| POST /query/execute | v0.7 제거 예정(초안) | **유지(deprecated)** | v4는 probe·smoke용 보존 |
| POST /meta/* | 초안 미기재 | v0.6과 동일 | 변경 없음 |

### `/data_decision` Request 변경 (+1)

| 필드 | v0.6 | v0.7 | 설명 |
|------|:----:|:----:|------|
| `query` | ✓ | ✓ | 동일 |
| `include_matched_columns` | ✓ | ✓ | 동일 |
| `column_top_m` | ✓ | ✓ | 동일 |
| `auto_resolve_entities` | — | **✓** | `true`(기본): PG db_probe로 코드 해소 시도 |

### `/data_decision` Response 변경 (+3)

| 필드 | v0.6 | v0.7 | 설명 |
|------|:----:|:----:|------|
| `meta_version` … `threshold_used` | ✓ | ✓ | **스키마 동일**, 값만 0.7 |
| `resolved_entities` | — | **✓** | 자연어 mention → 코드값 해소 결과 |
| `suggested_probes` | — | **✓** | 자동 해소 실패 시 1회 probe SQL 템플릿 |
| `resolution_status` | — | **✓** | `complete` \| `partial` \| `skipped` \| `failed` |

**v0.6 클라이언트 호환:** v0.7 응답은 v0.6 필드를 **모두 포함**. 신규 3필드는 빈 배열/`skipped` 기본값 → **파싱만 하위 호환**.

---

## 1. 공통 사항

| 항목 | v0.7 |
|------|------|
| Base URL | `http://<host>:8100` (Docker `robo-meta-api-v4`) |
| Content-Type | `application/json; charset=utf-8` |
| 버전 헤더 | `X-Meta-Version: 0.7` (모든 응답) |
| 빈 값 보장 | v0.6과 동일 — 미적재 시 `[]`, `null`, `"unknown"` |

### Endpoint 목록 (8 + health)

| Method | Path | meta_version | 비고 |
|--------|------|:------------:|------|
| GET | `/health` | 0.7 | |
| POST | `/data_decision` | 0.7 | **v0.7 확장** |
| POST | `/meta/batch` | 0.7 | v0.6 동일 |
| POST | `/meta/table` | 0.7 | v0.6 동일 |
| POST | `/meta/column` | 0.7 | v0.6 동일 |
| POST | `/meta/fk` | 0.7 | v0.6 동일 |
| POST | `/query/execute` | 0.7 | deprecated, v4에서 유지 |
| POST | `/query` | 0.7 | stub (`not_implemented`) |

---

## 2. POST /data_decision

자연어 질의 → (a) target 분기, (b) 테이블 후보, (c) join_groups, **(d) v0.7 entity resolution**.

### 2-1. Request

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|------|------|:----:|--------|------|
| `query` | string | ✓ | — | 자연어 질의 |
| `include_matched_columns` | boolean | | `true` | `matched_columns` 포함 여부 |
| `column_top_m` | integer | | null | 테이블별 컬럼 매칭 상한 (1–50) |
| `auto_resolve_entities` | boolean | | `true` | v0.7 A안: 1차 코드 해소 |

> **Request body에 `schema` / `schema_name` 없음.** PG·Neo4j 스키마 범위는 서버 env `DECISION_SCHEMA_ALLOWLIST`(예: `rwis`)로 제한. entity resolution DB 접속은 `SOURCE_PG_SCHEMA`, `PG_SCHEMAS`.

```json
{
  "query": "2025년 9월 15일 수지정수장 계측값 현황 알려줘",
  "include_matched_columns": true,
  "column_top_m": 10,
  "auto_resolve_entities": true
}
```

### 2-2. Response (top-level)

| 필드 | 타입 | v0.6 | v0.7 |
|------|------|:----:|:----:|
| `meta_version` | string | ✓ | `"0.7"` |
| `target` | string | ✓ | `analytic` \| `source` \| `collect` \| `none` |
| `secondary_targets` | array[string] | ✓ | |
| `confidence` | float | ✓ | 0.0–1.0 |
| `candidates` | array | ✓ | 아래 §2-3 |
| `join_groups` | array | ✓ | 아래 §2-4 |
| `threshold_used` | object | ✓ | HyDE/vector/join 파라미터 |
| `resolved_entities` | array | — | **§2-5** |
| `suggested_probes` | array | — | **§2-6** |
| `resolution_status` | string | — | **§2-7** |

### 2-3. `candidates[]` (v0.6 동일)

| 필드 | 타입 | 설명 |
|------|------|------|
| `db` | string | 원천 DB 라벨 (R-3 폴백) |
| `schema_name` | string | 스키마 |
| `table_name` | string | 테이블 |
| `score` | float | 벡터 유사도 |
| `source` | string | `vector` \| `schema_rule` \| `name_rule` |
| `target_class` | string | `analytic` \| `source` \| `collect` \| `mixed` \| `unknown` |
| `subject_area` | string | `agg` \| `raw` \| `code` \| `hist` \| `master` \| `link` \| `unknown` |
| `matched_columns` | array | §2-3-1 |

#### 2-3-1. `matched_columns[]`

| 필드 | 타입 |
|------|------|
| `column_name` | string |
| `score` | float |
| `constraints` | array[string] (`PK`, `FK`, `UNIQUE`) |
| `column_name_kr` | string \| null |
| `data_type` | string \| null |
| `description` | string \| null |

### 2-4. `join_groups[]` (v0.6 동일)

| 필드 | 타입 |
|------|------|
| `members` | array[{db, schema_name, table_name}] |
| `bridge_tables` | array |
| `cross_db` | boolean |
| `recommended_strategy` | string |
| `bridges` | array[{from, to, via, path, confidence}] |
| `group_score` | float |
| `score_breakdown` | object |
| `rationale` | string \| null |

`bridges[].via`: `fk` \| `ontology` \| `embedding` \| `term` \| **`convention`** (v4 join fallback)

### 2-5. `resolved_entities[]` (v0.7 신규)

| 필드 | 타입 | 설명 |
|------|------|------|
| `mention` | string | 질의에서 추출한 표면형 (예: `수지정수장`) |
| `entity_type` | string | `facility` \| `tag` \| `code` \| `unknown` |
| `db` | string \| null | 원천 DB 라벨 |
| `schema_name` | string \| null | 스키마 |
| `table` | string | 마스터/코드 테이블 |
| `name_column` | string | 이름 컬럼 (예: `SUJ_NAME`) |
| `code_column` | string \| null | 코드 컬럼 (예: `SUJ_CODE`) |
| `values` | array | §2-5-1 |
| `source` | string | `db_probe` \| `value_examples` |

#### 2-5-1. `values[]`

| 필드 | 타입 |
|------|------|
| `code` | string |
| `label` | string \| null |
| `confidence` | float |

### 2-6. `suggested_probes[]` (v0.7 신규)

자동 해소 실패(`partial`/`failed`) 시 외부 T2SQL이 **1회** `/query/execute`에 사용.

| 필드 | 타입 | 설명 |
|------|------|------|
| `purpose` | string | 기본 `code_lookup` |
| `sql` | string | probe SELECT |
| `expected_columns` | array[string] | |
| `maps_to_filter` | object \| null | `{mention: column}` |
| `reason` | string \| null | |

### 2-7. `resolution_status` (v0.7 신규)

| 값 | 의미 |
|----|------|
| `complete` | 모든 mention 코드 해소 |
| `partial` | 일부만 해소 |
| `skipped` | `auto_resolve_entities=false` 또는 키워드 없음 |
| `failed` | PG probe 불가 등 |

### 2-8. Response Example (v0.7, RWIS E2E)

> `candidates[].db`는 Neo4j DataSource 라벨(R-3 폴백 `META_DB_LABEL`). legacy ingest 잔존 시 `hwaseong` 등 **실제 PG DB명과 다를 수 있음**. `schema_name`·테이블·코드 해소가 핵심.

```json
{
  "meta_version": "0.7",
  "target": "source",
  "secondary_targets": ["analytic"],
  "confidence": 0.5,
  "candidates": [
    {
      "db": "hwaseong",
      "schema_name": "rwis",
      "table_name": "RDF01HH_TB",
      "score": 0.44,
      "source": "vector",
      "target_class": "analytic",
      "subject_area": "agg",
      "matched_columns": []
    }
  ],
  "join_groups": [],
  "threshold_used": {
    "analytic": 0.7,
    "source": 0.55,
    "topk": 10,
    "mode": "internal_hyde+vector",
    "join_groups_mode": "convention",
    "meta_version": "0.7"
  },
  "resolved_entities": [
    {
      "mention": "수지정수장",
      "entity_type": "code",
      "schema_name": "rwis",
      "table": "RDISAUP_TB",
      "name_column": "SUJ_NAME",
      "code_column": "SUJ_CODE",
      "values": [{ "code": "316", "label": "수지정수장", "confidence": 1.0 }],
      "source": "db_probe"
    }
  ],
  "suggested_probes": [],
  "resolution_status": "complete"
}
```

---

## 3. POST /query/execute (deprecated, v4 유지)

> v0.6 초안: v0.7 제거 예정. **v4는 `suggested_probes` probe 및 smoke용으로 유지.**

Request/Response body는 **v0.6 초안 §3과 동일**. `meta_version`만 `"0.7"`.

---

## 4. POST /meta/* (v0.6 동일)

초안(`api_spec_draft.md`)에 미포함. v4 구현은 v0.6 RC와 동일 스키마.

| Path | Request | Response |
|------|---------|----------|
| `/meta/batch` | `{ batch_date?: string }` | `{ meta_version, items[], total }` |
| `/meta/table` | `{ db?, schema_name, table_name }` | `MetaTableResponse` |
| `/meta/column` | `{ db?, schema_name, table_name, column_name }` | `{ meta_version, column }` |
| `/meta/fk` | `{ db?, schema_name, table_name }` | `{ meta_version, fk[] }` |

---

## 5. 외부 T2SQL 연동 (v0.7)

1. **필수 1회:** `POST /data_decision`
2. `resolution_status=complete` + `resolved_entities` 있음 → **종료** (WHERE 코드값 사용)
3. `suggested_probes`만 있음 → `POST /query/execute` **1회**
4. **금지:** 동일 질문으로 `/data_decision` 재호출

상세: [`external_t2sql_integration.md`](external_t2sql_integration.md)

---

## 6. 빈 값 보장 (v0.6 §5 계승)

| 경로 | 미적재 시 |
|------|-----------|
| `secondary_targets` | `[]` |
| `candidates[].matched_columns` | `[]` |
| `join_groups` | `[]` |
| `resolved_entities` | `[]` |
| `suggested_probes` | `[]` |
| `resolution_status` | `skipped` |

---

## 부록: Graph Contract

Neo4j 메타 그래프: [`kair_robo_meta_api_graph_contract.md`](kair_robo_meta_api_graph_contract.md)  
v0.7 entity resolution 선행: `Table.subject_area` ∈ `{master, code}`, PG `db_probe`.
