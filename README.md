# robo-meta-api v2

> Neo4j 베이스 v0.6 RC body 정합 meta-api — **v2 (브랜치: v0.2)**
>
> v1 대비 추가된 핵심 기능:
> - `/data_decision` 응답에 **FK 경로 기반 `join_groups` 탐색 로직** 추가 (Union-Find + `fkTo` 관계 그래프 탐색)
> - Neo4j Cypher 쿼리 최적화 (`properties(fk)['constraint']` 방식으로 정적 속성 경고 제거)
> - `/meta/*` 엔드포인트 양방향 관계 지원 (`belongsTo` + `HAS_TABLE` 동시 조회)
> - 포트 `8098` (v1: 8097 과 독립 운영)
> - `docs/` 폴더: 분석 보고서 및 가이드 문서 포함

---

## 1. 한 줄 정리

```
외부 AI ──[HTTP v0.6 body]──> robo-meta-api-v2(8098) ──[Cypher / psycopg]──> robo-neo4j(7687) + robo-postgres(5432)
                                  │
                                  └ /data_decision: FK 경로 탐색 → join_groups 조립 (v2 신규)
```

---

## 2. 빠른 시작

### 전제 — 아래 컨테이너가 실행 중이어야 함

- `robo-neo4j` (`neo4j:2025.11.2-community`, 7687) — 메타 그래프 (`text_to_sql_table_vec_index` ONLINE)
- `robo-postgres` (`postgres:15-alpine`, 5432) — 원천 RDB `rwis` DB / `RWIS` 스키마

### Docker (권장)

```bash
git clone -b v0.2 <this-repo>
cd robo-meta-api
cp .env.example .env
# .env 에 필요한 접속 정보 입력 (NEO4J_URI, SOURCE_PG_*, OPENAI_API_KEY 등)
docker compose up -d --build
docker logs -f robo-meta-api-v2
curl http://127.0.0.1:8098/health
```

### 로컬 직접 실행

```bash
pip install -r requirements.txt
python -m app.main
# -> http://127.0.0.1:8098
```

### 회귀 테스트

```bash
python tests/smoke_v06.py        # 8 endpoint 200 + meta_version=0.6
```

---

## 3. 디렉토리 구조

```
robo-meta-api/                         (v0.2 브랜치)
├── app/
│   ├── main.py                        # FastAPI 부팅 + lifespan (Neo4j + 원천 PG)
│   ├── config.py                      # 통합 settings (Neo4j + 원천 PG + decision + exec)
│   ├── db.py                          # Neo4j 드라이버 + 원천 PG psycopg 풀
│   ├── schemas.py                     # K-AIR v0.6 RC 스펙 (JoinGroup / JoinBridge 포함)
│   ├── routers/
│   │   ├── decision.py                # POST /data_decision
│   │   ├── meta.py                    # POST /meta/{batch,table,column,ref}
│   │   └── query_exec.py              # POST /query/execute
│   ├── services/
│   │   ├── decision_service.py        # HyDE+벡터 → FK 경로 탐색 → JoinGroup 조립 (v2 핵심)
│   │   ├── meta_service.py            # /meta/* Cypher (양방향 관계 지원)
│   │   ├── subject_area.py            # 분류 규칙
│   │   ├── query_runner.py            # READ ONLY TX + 감사 JSONL
│   │   ├── sql_guard.py               # SELECT 화이트리스트 / DDL 블랙리스트
│   │   └── neo4j_client/              # HyDE, embedding, vector_search, db_probe
│   └── rules/subject_area_rules.yaml  # 분류 규칙 YAML
├── tests/smoke_v06.py                 # VER-MN-04 (8 endpoint 회귀)
├── docs/                              # 분석 보고서 및 가이드 문서
│   ├── fk_path_expansion_plan.md      # FK 경로 탐색 설계 계획서
│   ├── fk_path_expansion_v2_report.md # v2 작업 완료 보고서
│   ├── kair_ontology_fk_investigation.md # KAIR 온톨로지/FK fallback 조사
│   ├── metadata_mapping_analysis.md   # Neo4j 메타데이터 구조 분석
│   ├── rag_comparison_report.md       # RAG 비교 검증 보고서
│   └── query_execute_backend_guide.md # /query/execute 백엔드 교체 가이드
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

---

## 4. 8 endpoint × 응답 키 (v0.6 RC body)

| Method | Path | 응답 핵심 키 |
|--------|------|-------------|
| GET | `/health` | `meta_version=0.6` + `X-Meta-Version: 0.6` 헤더 |
| POST | `/data_decision` | `target / secondary_targets / confidence / candidates[].matched_columns / join_groups / threshold_used` |
| POST | `/meta/batch` | `items[].db / schema_name / table_name` |
| POST | `/meta/table` | `table_info / columns / fk` |
| POST | `/meta/column` | `column.{column_name, data_type, constraints, ...}` |
| POST | `/meta/ref` | `fk[].{column_name, ref_schema_name, ref_table_name, ref_column_name}` |
| POST | `/query/execute` | `status / sql_executed / columns / rows / row_count / truncated / elapsed_ms` (sql_guard 차단 시 400) |

---

## 5. v2 핵심 변경: FK 경로 탐색 (`join_groups`)

### 동작 방식

`/data_decision` 호출 시 벡터 검색으로 선별된 후보 테이블 간 FK 연결 경로를 탐색하여 `join_groups`를 조립합니다.

```
후보 테이블 선별 (HyDE + 벡터 검색)
        │
        ▼
Neo4j: (Column)-[:fkTo]->(Column) 관계 조회
        │
        ▼
Union-Find 클러스터링 → FK로 연결된 테이블 그룹화
        │
        ▼
JoinGroup / JoinBridge 객체 조립 → join_groups 반환
```

### 응답 예시

```json
{
  "join_groups": [
    {
      "tables": ["rwis.table_a", "rwis.table_b"],
      "bridges": [
        {
          "from_table": "rwis.table_a",
          "from_column": "col_id",
          "to_table": "rwis.table_b",
          "to_column": "a_id",
          "constraint": "fk_b_to_a"
        }
      ]
    }
  ]
}
```

> **참고**: FK 관계가 Neo4j에 적재되지 않은 테이블 간에는 `join_groups`가 빈 배열(`[]`)로 반환됩니다.

---

## 6. 폴백 정책

- **OPENAI 미설정 / quota 초과**: HyDE + 임베딩 skip → keyword Cypher 폴백  
  (`MATCH (t:Table) WHERE toLower(name/desc/analyzed) CONTAINS kw`)
- **FK 관계 미적재**: `join_groups: []` 반환 (정상 동작)
- **`db` 라벨 폴백 체인**: `Schema.db || DataSource.engine || META_DB_LABEL` 환경변수

---

## 7. 환경변수 (.env)

```dotenv
# Neo4j
NEO4J_URI=bolt://host.docker.internal:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=

# 원천 PostgreSQL
SOURCE_PG_HOST=host.docker.internal
SOURCE_PG_PORT=5432
SOURCE_PG_DB=rwis
SOURCE_PG_USER=postgres
SOURCE_PG_PASS=
SOURCE_PG_SCHEMA=RWIS

# OpenAI (HyDE + 임베딩 활성화 — 비워두면 keyword 폴백)
OPENAI_API_KEY=

# API
API_PORT=8098

# 기타 정책
META_DB_LABEL=kwater_prod
DECISION_VECTOR_TOPK=10
```

---

## 8. 컴포넌트 출처

| 원천 | 차용 모듈 | 변경 |
|------|----------|------|
| `test_K_Water/neo4j_client/` | `app/services/neo4j_client/*` | OPENAI_API_KEY 하드코딩 제거, `vector_search.py` Cypher 최적화 |
| `K-AIR-meta-api` | `schemas.py`, `sql_guard.py`, `query_runner.py`, `subject_area.py`, `query_exec.py`, `subject_area_rules.yaml` | 최소 수정 |
| 신규 (v1) | `main.py`, `config.py`, `db.py`, `decision_service.py`, `meta_service.py`, `routers/` | 신규 작성 |
| 신규 (v2) | `decision_service.py` FK 탐색 로직, `meta_service.py` 양방향 관계 | v2 추가 |

---

## 9. 알려진 제약

- **FK 미적재**: 현재 Neo4j에 `fkTo` 관계가 없는 테이블 쌍은 `join_groups`가 비어 있음. Neo4j에 FK 관계 적재 시 자동 반영.
- **`Schema.db=oracle` 표기**: 원천은 PostgreSQL이나 Neo4j 노드의 `db` 속성이 `oracle`. `META_DB_LABEL` 환경변수로 통제 가능.
- **OPENAI quota**: API 키 미설정 또는 quota 초과 시 keyword Cypher 폴백 동작.
- **`/query/execute` 백엔드 교체**: 현재 PostgreSQL 전용. MindsDB 등으로 교체 시 `docs/query_execute_backend_guide.md` 참조.

---

## 10. 라이선스

Private. K-water 내부 사용.
