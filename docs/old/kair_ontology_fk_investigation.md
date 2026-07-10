# KAIR Text2SQL RAG - FK 미연결 시 온톨로지 기반 테이블 탐색 로직 조사 보고서

작성일: 2026-06-13

---

## 1. 조사 목적

KAIR의 text2SQL RAG 또는 자연어 질의 서비스 중, **FK(외래키) 연결이 없을 경우 온톨로지(Ontology)를 적용하여 의미적으로 관련 있는 테이블을 추가 호출하는 로직**이 존재하는지 파악.

---

## 2. 조사 대상 코드 경로

| 경로　　　　　　　　　　　　　　　　　　　　　　　　　　　　　　　　　　　　　　　 | 설명　　　　　　　　　　　　　　　　　　　　　　　 |
| ------------------------------------------------------------------------------------| ----------------------------------------------------|
| `KAIR/neo4j-text2sql/app/react/tools/build_sql_context_parts/orchestrator.py`　　　| RAG 파이프라인 진입점 (전체 플로우 오케스트레이터) |
| `KAIR/neo4j-text2sql/app/react/tools/build_sql_context_parts/fk_flow.py`　　　　　 | FK 관계 조회 및 XML 출력　　　　　　　　　　　　　 |
| `KAIR/neo4j-text2sql/app/react/tools/build_sql_context_parts/table_search_flow.py` | 테이블 벡터 검색 + 리랭크　　　　　　　　　　　　　|
| `KAIR/neo4j-text2sql/app/react/tools/build_sql_context_parts/similar_flow.py`　　　| 유사 쿼리 검색, 값 매핑　　　　　　　　　　　　　　|
| `KAIR/neo4j-text2sql/app/core/graph_search.py`　　　　　　　　　　　　　　　　　　 | Neo4j FK 경로 탐색 (GraphSearcher)　　　　　　　　 |
| `KAIR/neo4j-text2sql/app/routers/schema_edit.py`　　　　　　　　　　　　　　　　　 | FK_TO_TABLE 관계 수동 등록/조회 API　　　　　　　　|

---

## 3. KAIR RAG 파이프라인 구조 (요약)

```
질의 입력
   │
   ├─ [1] 임베딩 생성 (question_embedding)
   │
   ├─ [2] HyDE 생성 + 유사 쿼리 검색 (병렬)
   │       └─ 벡터 유사도 기반 과거 질의 매핑
   │
   ├─ [3] 테이블 검색 + 리랭크 (table_search_flow)
   │       └─ 벡터 인덱스 `table_vec_index` 조회
   │
   ├─ [4] FK 관계 조회 (_neo4j_fetch_fk_relationships)
   │       └─ 선별 테이블 간 Column [:FK_TO] 관계 탐색
   │
   ├─ [5] 컬럼 검색 (column_search_flow)
   │
   ├─ [6] 해결 값 매핑 (resolved_values_flow)
   │
   ├─ [7] 제안 생성 (suggestions_flow)
   │
   └─ [8] 경량 SQL 실행 (light_queries_flow)
```

---

## 4. 조사 결과

### 4.1 온톨로지 기반 fallback 로직: **존재하지 않음**

- 전체 코드베이스에서 `ontolog` 키워드 검색 결과: **0건**
- FK 미연결 시 의미적으로 관련 테이블을 끌어오는 별도 로직 없음
- `fk_flow.py`는 단순히 선별된 테이블 간 `:FK_TO` 관계를 조회하고, 없으면 **빈 결과 반환** 후 종료

### 4.2 FK 관계 탐색 방식: **Column-level FK_TO 관계**

`fk_flow.py` → `_neo4j_fetch_fk_relationships()` 내부:

```cypher
MATCH (t1:Table)-[:HAS_COLUMN]->(c1:Column)-[fk:FK_TO]->(c2:Column)<-[:HAS_COLUMN]-(t2:Table)
WHERE (t1.schema + '.' + t1.name) IN $table_fqns
  AND (t2.schema + '.' + t2.name) IN $table_fqns
RETURN ...
```

- 탐색 단위: **Column 노드 간 `:FK_TO` 관계**
- 조건: **선별된 테이블 집합 내부에서만** 탐색 (외부 테이블로 확장 없음)

### 4.3 Table-level FK_TO_TABLE 관계: **graph_search.py에 존재하나 미활용**

`GraphSearcher.find_fk_paths()` (graph_search.py:178):

```cypher
MATCH (t1:Table)-[r:FK_TO_TABLE*..3]-(t2:Table)
WHERE (t1.db + '|' + t1.schema + '|' + t1.name) IN keys
  AND (t2.db + '|' + t2.schema + '|' + t2.name) IN keys
```

- **Table 노드 간 `:FK_TO_TABLE` 관계를 최대 3홉까지 탐색**하는 기능이 정의되어 있음
- 그러나 현재 RAG 파이프라인(`orchestrator.py`)은 이 `GraphSearcher`를 **호출하지 않음**
- `GraphSearcher`는 별도 레거시 경로(`graph_search.py`)에 존재하며, 실 파이프라인에서 비활성 상태

### 4.4 FK_TO_TABLE 수동 등록 API: **존재, 활용 가능**

`schema_edit.py` (`/schema-edit/relationships` POST):

```json
{
  "from_table": "테이블A",
  "from_schema": "rwis",
  "sourceColumn": "컬럼A",
  "to_table": "테이블B",
  "to_schema": "rwis",
  "targetColumn": "컬럼B",
  "type": "many_to_one",
  "description": "관계 설명"
}
```

- 사용자가 수동으로 FK 관계를 Neo4j에 `source='user'`로 등록 가능
- 등록된 관계는 `source` 구분: `ddl` / `user` / `procedure`
- **단, 현재 RAG 파이프라인은 FK_TO_TABLE을 FK_TO (column-level)로 변환하지 않기 때문에, 수동 등록해도 fk_flow.py 검색에 반영되지 않음**

---

## 5. 결론 및 현황 정리

| 항목 | 현황 |
|------|------|
| 온톨로지 기반 관련 테이블 추가 탐색 | **없음** |
| FK 미연결 시 fallback 로직 | **없음 (빈 배열 반환)** |
| Column-level FK_TO 탐색 | 존재, 선별 테이블 내부만 탐색 |
| Table-level FK_TO_TABLE 그래프 탐색 | 코드 정의 있으나 파이프라인에서 **비활성** |
| FK_TO_TABLE 수동 등록 API | 존재하나 fk_flow.py와 **미연동** |

---

## 6. 시사점 및 권고사항

### 6.1 `robo-meta-api-v2`와의 비교

| 기능 | KAIR Text2SQL RAG | robo-meta-api-v2 |
|------|-------------------|-----------------|
| FK 탐색 단위 | Column-level `FK_TO` | Column-level `fkTo` (동일) |
| 선별 테이블 간 FK 경로 탐색 | 선별 집합 내부만 | Union-Find 기반 전체 탐색 |
| FK 없을 때 fallback | 없음 | 없음 (동일) |
| 온톨로지 기반 확장 | 없음 | 없음 |

### 6.2 개선 권고

FK 연결 없는 테이블 간 관련성을 확보하려면 다음 방안 중 하나를 검토:

1. **`FK_TO_TABLE` 수동 등록 + fk_flow.py 연동 개선**
   - `schema_edit.py` API로 논리적 관계 등록
   - `fk_flow.py`에서 `FK_TO_TABLE`도 함께 조회하도록 Cypher 수정

2. **GraphSearcher 활성화**
   - `orchestrator.py`에 `GraphSearcher.find_fk_paths()` 호출 추가
   - FK 없는 경우 3홉 이내 `FK_TO_TABLE` 경로로 테이블 확장

3. **임베딩 유사도 기반 테이블 공동 선별 (현재 방식)**
   - 별도 온톨로지 없이 벡터 검색으로 연관 테이블 묶음 선별
   - 한계: FK 경로 정보 없으므로 JOIN 힌트 제공 불가

---

*보고서 끝*
