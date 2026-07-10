# robo-meta-api Generic Config화 계획서

> 버전: 0.1 (draft)  
> 작성일: 2026-06-13  
> 전제: `docs/kair_robo_meta_api_graph_contract.md` Contract v0.1  
> 목적: Neo4j 메타 **내용·관계 풍부화(A등급)** 는 KAIR에서,  
>       rel type·정책 변경 **(B등급)** 은 **코드 재배포 없이 env/config** 로 반영

---

## 1. 배경

| 문제 | generic config로 해결 |
|------|------------------------|
| `_ONTOLOGY_REL_TYPES` 코드 하드코딩 | DB/배포마다 다른 Table rel → **env CSV** |
| `dataFlowsTo` 등 기존 rel 미조회 | env에 추가만 |
| convention exclude가 RWIS 관용어 고정 | env JSON/CSV |
| 벡터 인덱스명 변경 시 코드 수정 | env `NEO4J_TABLE_VECTOR_INDEX` |
| multi-DB 혼재 그래프 | (선택) schema allowlist env |

**비목표:** RWIS·특정 schema 하드코딩, Neo4j 적재 로직 API 내장.

---

## 2. 변경 등급 B — 본 계획 범위

Contract §7 **B등급**: API **설정만** 변경.

```
KAIR: Neo4j에 rel/메타 추가 (A+B)
        ↓
운영: .env / docker-compose environment 갱신
        ↓
robo-meta-api 재시작 (이미지 rebuild 불필요)
        ↓
smoke / data_decision join 검증
```

---

## 3. 현재(v3) vs 목표(To-Be) env 매트릭스

| env 변수 | v3 상태 | To-Be | B등급 |
|----------|---------|-------|-------|
| `JOIN_ONTOLOGY_ENABLED` | ✅ 구현 | 유지 | ✅ |
| `JOIN_CONVENTION_ENABLED` | ✅ 구현 | 유지 | ✅ |
| `JOIN_CONVENTION_MIN_COL_LEN` | ✅ 구현 | 유지 | ✅ |
| `JOIN_CONVENTION_CONFIDENCE` | ✅ 구현 | 유지 | ✅ |
| `JOIN_CONVENTION_CONFIDENCE_MISMATCH` | ✅ 구현 | 유지 | ✅ |
| `JOIN_ONTOLOGY_REL_TYPES` | ❌ 코드 상수 | **신규** | ✅ 핵심 |
| `JOIN_CONVENTION_EXCLUDE` | ❌ 코드 상수 | **신규** | ✅ |
| `JOIN_FK_LIMIT` | ❌ 하드코딩 50 | **신규** | ✅ |
| `JOIN_ONTOLOGY_LIMIT` | ❌ 하드코딩 50 | **신규** | ✅ |
| `JOIN_CONVENTION_LIMIT` | ❌ 하드코딩 30 | **신규** | ✅ |
| `NEO4J_TABLE_VECTOR_INDEX` | ❌ 코드 상수 | **신규** | ✅ |
| `DECISION_SCHEMA_ALLOWLIST` | ❌ 미구현 | **선택** | ✅ |
| `META_DB_LABEL` | ✅ 구현 | 유지 | ✅ |
| `DECISION_VECTOR_TOPK` 등 | ✅ 구현 | 유지 | ✅ |

---

## 4. 신규 env 상세 스펙

### 4-1. `JOIN_ONTOLOGY_REL_TYPES` (필수)

Table↔Table 2단 join에 사용할 rel type CSV.

```dotenv
# default (v3 코드와 동일 + dataFlowsTo)
JOIN_ONTOLOGY_REL_TYPES=fkToTable,FK_TO_TABLE,RELATED_TO,REFERENCES,DOMAIN_RELATED,dataFlowsTo
```

**파싱:** comma-separated, trim, lower 비교 없음 (Neo4j type case-sensitive)

**적용:** `vector_search.fetch_ontology_relationships()` → `settings.join_ontology_rel_types`

### 4-2. `JOIN_CONVENTION_EXCLUDE`

```dotenv
JOIN_CONVENTION_EXCLUDE=id,seq,yn,use_yn,del_yn,reg_dt,upd_dt,reg_id,upd_id,remark,memo,note
```

**파싱:** comma-separated → lowercase set

### 4-3. Limit 계열

```dotenv
JOIN_FK_LIMIT=50
JOIN_ONTOLOGY_LIMIT=50
JOIN_CONVENTION_LIMIT=30
```

### 4-4. `NEO4J_TABLE_VECTOR_INDEX`

```dotenv
NEO4J_TABLE_VECTOR_INDEX=text_to_sql_table_vec_index
```

Contract §5 인덱스명 변경 시 B등급 대응.

### 4-5. `DECISION_SCHEMA_ALLOWLIST` (선택, Phase 2)

```dotenv
# 비어 있으면 전 schema 검색 (현재 동작)
DECISION_SCHEMA_ALLOWLIST=rwis,public
```

벡터 검색 `schema_filter`에 전달 — **multi-DB 혼재 그래프**에서 후보 pool 제한.

---

## 5. 구현 Step (robo-meta-api v3.1)

### Step 1. `app/config.py`

```python
def _csv_set(name: str, default: str) -> frozenset[str]:
    raw = _env(name, default)
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())

def _csv_list(name: str, default: str) -> tuple[str, ...]:
    raw = _env(name, default)
    return tuple(x.strip() for x in raw.split(",") if x.strip())

# 추가 필드
join_ontology_rel_types: tuple[str, ...] = _csv_list(
    "JOIN_ONTOLOGY_REL_TYPES",
    "fkToTable,FK_TO_TABLE,RELATED_TO,REFERENCES,DOMAIN_RELATED,dataFlowsTo",
)
join_convention_exclude: frozenset[str] = _csv_set(
    "JOIN_CONVENTION_EXCLUDE",
    "id,seq,yn,use_yn,del_yn,reg_dt,upd_dt,reg_id,upd_id,remark,memo,note",
)
join_fk_limit: int = int(_env("JOIN_FK_LIMIT", "50"))
join_ontology_limit: int = int(_env("JOIN_ONTOLOGY_LIMIT", "50"))
join_convention_limit: int = int(_env("JOIN_CONVENTION_LIMIT", "30"))
neo4j_table_vector_index: str = _env("NEO4J_TABLE_VECTOR_INDEX", "text_to_sql_table_vec_index")
decision_schema_allowlist: tuple[str, ...] = _csv_list("DECISION_SCHEMA_ALLOWLIST", "")
```

### Step 2. `vector_search.py`

- `_ONTOLOGY_REL_TYPES` 삭제 → `settings.join_ontology_rel_types` 사용
- `_CONVENTION_EXCLUDE` 삭제 → `settings.join_convention_exclude` 사용
- `_TEXT2SQL_TABLE_INDEX` → `settings.neo4j_table_vector_index`

### Step 3. `decision_service.py`

- `fetch_*` limit → `settings.join_*_limit`
- `search_tables_by_vector` 호출 시:
  ```python
  schema_filter=settings.decision_schema_allowlist or None
  ```

### Step 4. `docker-compose.yml` / `.env.example` / `README.md`

env 블록 및 표 갱신.

### Step 5. `tests/test_config_join.py` (신규, 경량)

- `_csv_list`, `_csv_set` 파싱 unit test
- mock settings로 rel types 전달 검증 (Neo4j 불필요)

---

## 6. 배포별 config 예시 (DB-agnostic)

### 예 A — Table rel 풍부, convention 보수

```dotenv
JOIN_ONTOLOGY_REL_TYPES=fkToTable,dataFlowsTo,REFERENCES
JOIN_ONTOLOGY_ENABLED=1
JOIN_CONVENTION_ENABLED=0
```

### 예 B — FK sparse, convention fallback

```dotenv
JOIN_ONTOLOGY_ENABLED=0
JOIN_CONVENTION_ENABLED=1
JOIN_CONVENTION_MIN_COL_LEN=5
JOIN_CONVENTION_EXCLUDE=id,seq,yn,reg_dt,upd_dt,created_at,updated_at
```

### 예 C — multi-schema 그래프, 후보 제한

```dotenv
DECISION_SCHEMA_ALLOWLIST=rwis,water_stat
JOIN_ONTOLOGY_REL_TYPES=fkToTable,dataFlowsTo
```

> schema 이름은 **배포마다 다름** — RWIS 전용 preset 아님.

---

## 7. KAIR ↔ API 협업 Runbook (B등급)

```
1. KAIR: Neo4j에 새 rel type 적재 (예: dataFlowsTo)
2. KAIR: Contract 부록 rel inventory Cypher 결과 공유
3. API 운영: JOIN_ONTOLOGY_REL_TYPES 에 type 추가
4. docker compose restart robo-meta-api-v3  (rebuild 불필요)
5. python tests/test_data_decision_join.py
6. join_groups_mode 에 ontology/fk 포함 확인
```

**코드 PR 불필요** — env + restart만.

---

## 8. 검증 계획

| ID | 검증 | 기대 |
|----|------|------|
| G-1 | env rel type 추가 후 restart | 2단 Cypher가 새 type 반환 |
| G-2 | `JOIN_CONVENTION_ENABLED=0` | convention bridge 0 |
| G-3 | `JOIN_ONTOLOGY_REL_TYPES=` (empty) | ontology 0건, fk/convention만 |
| G-4 | `DECISION_SCHEMA_ALLOWLIST` | 후보 schema subset |
| G-5 | Contract checklist Cypher | vec/dtype/fkTo pass |

---

## 9. 일정·우선순위

| Phase | 항목 | effort |
|-------|------|--------|
| **P0** | `JOIN_ONTOLOGY_REL_TYPES`, limits | S (~半日) |
| **P1** | `JOIN_CONVENTION_EXCLUDE`, vector index env | S |
| **P2** | `DECISION_SCHEMA_ALLOWLIST` | M |
| **P3** | config unit test, RUNBOOK | S |

---

## 10. 기대 효과 (질문에 대한 답)

**Q: generic config 후 다른 DB Neo4j 적재 시 더 정확한 메타 제공?**

| 레이어 | config만 | + KAIR 메타 증강 |
|--------|-----------|------------------|
| rel type 반영 | ✅ 재시작으로 즉시 | ✅ |
| 후보 테이블 정확도 | △ (schema allowlist 정도) | ✅ embedding/description |
| join hint (fk/ontology) | ✅ rel 있으면 | ✅ rel + 올바른 후보 overlap |
| convention noise | △ exclude/limit 튜닝 | ✅ fk/ontology ↑ → 3단 의존 ↓ |

**한 줄:** config = **같은 API로 어떤 DB 그래프든 소비**; 정확도 ↑ = **config + Neo4j 증강(A등급)** 병행.

---

## 11. 관련 문서

- `docs/kair_robo_meta_api_graph_contract.md`
- `docs/agent_join_hints.md`
- `docs/robo-meta-api-v3-plan-v2.md`
