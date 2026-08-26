# KAIR ↔ robo-meta-api 그래프 Contract (초안)

> **현행 아님 (2026-08-26).** 이 초안은 Neo4j 적재·포트 **8099**·`/query/execute` 기준이다.
> 지금 메타 SoT는 K-AIR Store `t2s_*`(포트 **8100**, `POST /query_execute`)이다.
> 기동은 [`../RUNBOOK.md`](../RUNBOOK.md).

> 버전: 0.1 (draft)  
> 작성일: 2026-06-13  
> 적용 대상: robo-meta-api v3 (`8099`)  
> 목적: **Neo4j 메타 적재(KAIR / air-swmm)** 와 **API 소비(robo-meta-api)** 사이의  
>       **변경·협의 기준**을 정의한다. 특정 DB(RWIS) 전용 규칙이 아니다.

---

## 1. Contract란?

| 구분 | 설명 |
|------|------|
| **Contract (본 문서)** | API가 **읽는** 노드·관계·속성·인덱스의 **최소·권장 스펙** |
| **Contract 밖** | Entity/Plant/Equipment 온톨로지 등 — NLQ·air-swmm용, API join 2단 **미소비** |
| **변경 등급** | A(내용만) / B(config) / C(코드) — §7 참조 |

**원칙:** KAIR 쪽은 Contract를 **만족·풍부화**, robo-meta-api는 Contract 범위 내에서 **generic 소비**.

---

## 2. 시스템 경계

```
┌─────────────────────────────────────┐
│  KAIR / air-swmm / ETL (Producer)    │
│  - 메타 추출, AI 증강, embedding     │
│  - fkTo / fkToTable / 온톨로지 부여  │
└──────────────┬──────────────────────┘
               │  Graph Contract
               ▼
┌─────────────────────────────────────┐
│  Neo4j (메타 그래프)                 │
└──────────────┬──────────────────────┘
               │  Cypher / Vector Index
               ▼
┌─────────────────────────────────────┐
│  robo-meta-api (Consumer)            │
│  - /data_decision, /meta/*           │
│  - join_groups 3단 fallback          │
└──────────────┬──────────────────────┘
               │  K-AIR v0.6 HTTP body
               ▼
┌─────────────────────────────────────┐
│  외부 T2SQL / NLQ Agent              │
└─────────────────────────────────────┘
```

- **원천 SQL 실행** (`/query/execute`)은 Neo4j Contract와 **별도** — `SOURCE_PG_*` 단일 인스턴스 연결.

---

## 3. 노드 (Labels & Properties)

### 3-1. `:DataSource` (선택, 권장)

| 속성 | 필수 | API 사용처 |
|------|------|------------|
| `name` / `id` | 권장 | `/meta/*` DataSourceInfo |
| `engine` | 권장 | `db` 라벨 폴백 (`Schema.db` 없을 때) |

### 3-2. `:Schema`

| 속성 | 필수 | API 사용처 |
|------|------|------------|
| `name` | **필수** | FQN, `/meta/batch`, 후보 `schema_name` |
| `db` | 권장 | `db` 식별 (R-3 폴백 1순위) |

**관계 (Schema 쪽):**

- `(DataSource)-[:HAS_SCHEMA]->(Schema)` — `/meta/*` db 라벨 해석

### 3-3. `:Table` (핵심)

| 속성 | 필수 | API 사용처 |
|------|------|------------|
| `name` | **필수** | FQN, 벡터 검색, `/meta/table` |
| `schema` | **필수** | FQN = `schema.name` (schema 비어 있으면 name만) |
| `description` | 권장 | 벡터·keyword fallback |
| `analyzed_description` | 권장 | HyDE/벡터 품질 |
| `text_to_sql_vector` | **벡터 검색 필수** | `text_to_sql_table_vec_index` |
| `text_to_sql_db_exists` | 권장 | `false`면 검색·batch **제외** (기본 `true`) |
| `original_name` | 선택 | `fetch_anchor_columns` 매칭 |

**FQN 규칙 (API 내부):**

```
fqn = lower(schema + "." + name)   if schema non-empty
      lower(name)                  otherwise
```

**Producer 권장:**

- 스키마명 **canonical** 유지 (동일 테이블 중복 노드·이질 schema 문자열 지양)
- embedding 차원 = OpenAI `text-embedding-3-small` → **1536** (API default)

### 3-4. `:Column`

| 속성 | 필수 | API 사용처 |
|------|------|------------|
| `name` | **필수** | `/meta/column`, convention join |
| `dtype` | 권장 | `/meta/column`, convention `path` dtype |
| `description` | 권장 | anchor column 매칭 |

---

## 4. 관계 (Relationships)

### 4-1. API가 **직접 소비**하는 관계

| 관계 | 방향 | 용도 | API endpoint / 단계 |
|------|------|------|---------------------|
| `HAS_TABLE` / `belongsTo` | Table ↔ Schema | 테이블 소속 | `/meta/*` |
| `HAS_SCHEMA` | DataSource → Schema | db 라벨 | `/meta/*` |
| `HAS_COLUMN` / `hasColumn` | Table → Column | 컬럼 목록, convention | `/meta/*`, 3단 |
| `fkTo` | Column → Column | FK | `/meta/*`, **1단** join |
| `fkToTable` 등 Table↔Table | Table ↔ Table | 온톨로지 join | **2단** join (config rel list) |

> v3 2단 default rel types: `fkToTable`, `FK_TO_TABLE`, `RELATED_TO`, `REFERENCES`, `DOMAIN_RELATED`  
> (코드 상수 — **generic config화 예정**, § companion plan)

### 4-2. API가 **현재 소비하지 않는** 관계 (참고)

| 예시 | 비고 |
|------|------|
| `belongsToSystem`, `feedsTo`, `dataFlowsTo` | air-swmm·Entity 온톨로지. **Table join 2단 default 미포함** (`dataFlowsTo`는 config 추가 시 소비 가능) |
| `_Entity`, `Equipment`, `Plant` | 설비·조직 온톨로지 — T2SQL table contract **범위 밖** |

**확장 시:** Table↔Table join hint로 쓸 rel은 **Contract 부록( rel type 목록 )** 에 등재 후 config 반영.

---

## 5. 벡터 인덱스

| 항목 | Contract 값 |
|------|-------------|
| 인덱스 이름 | `text_to_sql_table_vec_index` |
| 대상 label | `:Table` |
| 벡터 property | `text_to_sql_vector` |
| 차원 | **1536** (API `OPENAI_EMBEDDING_DIM` default) |

인덱스 없을 시 API는 **cosine scan fallback** (성능 저하, 기능은 유지).

---

## 6. API endpoint ↔ Cypher 의존 매트릭스

| Endpoint | Neo4j 의존 |
|----------|------------|
| `GET /health` | 연결만 |
| `POST /data_decision` | Table vector, Column anchor, fkTo, Table rel (2·3단) |
| `POST /meta/batch` | Table–Schema–DataSource, `text_to_sql_db_exists` |
| `POST /meta/table` | Table, Column, fkTo, Schema |
| `POST /meta/column` | Table, Column, fkTo |
| `POST /meta/ref` | fkTo 전체 |
| `POST /query/execute` | **Neo4j 미사용** (SOURCE_PG) |

---

## 7. 변경 등급 (Change Classification)

Producer(KAIR) 변경 시 robo-meta-api 대응:

| 등급 | Neo4j 변경 예 | robo-meta-api |
|------|---------------|---------------|
| **A — 내용** | description 보강, embedding 재생성, fkTo/fkToTable **추가** | **변경 없음** |
| **B — config** | 새 Table↔Table rel type, convention exclude, threshold | **env/config만** (코드 배포 없이 가능 목표) |
| **C — contract** | label/rename (`Table`→다른 label), property rename, 인덱스명 변경, FQN 규칙 변경 | **코드 + Contract 버전 bump** |

**협의 절차 (권장):**

1. KAIR: 변경 등급 self-tag (A/B/C)
2. B → `JOIN_ONTOLOGY_REL_TYPES` 등 env 갱신 + smoke
3. C → Contract 버전 협의 → robo-meta-api PR + KAIR 적재 일정 맞춤

---

## 8. 품질 체크리스트 (Producer / KAIR)

신규 DB 또는 메타 증강 완료 후:

```cypher
-- 1) Table 수 & vector 적재율
MATCH (t:Table) WHERE COALESCE(t.text_to_sql_db_exists, true)
RETURN count(t) AS tables,
       count(t.text_to_sql_vector) AS with_vec,
       round(100.0*count(t.text_to_sql_vector)/count(t),1) AS vec_pct;

-- 2) Column dtype 적재율
MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)
RETURN count(c) AS cols, count(c.dtype) AS with_dtype;

-- 3) join 1단 fkTo
MATCH ()-[r:fkTo]->() RETURN count(r) AS fkTo_cnt;

-- 4) join 2단 Table rel (배포 env rel list와 대조)
MATCH (t1:Table)-[r]-(t2:Table) WHERE id(t1) < id(t2)
RETURN DISTINCT type(r) AS rel_type, count(*) AS cnt ORDER BY cnt DESC;

-- 5) Schema canonical (중복·혼재 탐지)
MATCH (t:Table) RETURN t.schema, count(*) AS cnt ORDER BY cnt DESC LIMIT 20;
```

**Pass 기준 (가이드):** `vec_pct` ≥ 80%, dtype ≥ 70%, 후보 질의 smoke에서 `join_groups_mode` ≠ `empty` (fk/ontology/convention 중 하나).

---

## 9. v0.6 응답 매핑 (요약)

| Neo4j | HTTP 응답 |
|-------|-----------|
| Table + Schema + DataSource | `DecisionCandidate.db / schema_name / table_name` |
| Table vector score | `candidates[].score` |
| fkTo | `join_groups[].bridges[].via = "fk"` |
| Table rel (2단) | `via = "ontology"`, `path = [rel_type]` |
| shared column (3단) | `via = "convention"`, `path = [shared_column:, dtype:, cast_recommended:]` |
| — | `threshold_used.join_groups_mode` |

미적재 필드 (`lineage_brief`, `ontology_anchors` 등) → API **null / []** 폴백 (v0.6 스펙 유지).

---

## 10. Contract 버전 이력

| 버전 | 날짜 | 변경 |
|------|------|------|
| 0.1 | 2026-06-13 | 초안 — v3 코드 기준 역추출 |

---

## 11. 관련 문서

- `docs/robo-meta-api-generic-config-plan.md` — B등급 config화 계획
- `docs/agent_join_hints.md` — T2SQL Agent bridge 소비 규칙
- `docs/robo-meta-api-v3-plan-v2.md` — v3 join fallback 구현 계획
