# robo-meta-api

> Neo4j 베이스 v0.6 RC body 정합 meta-api (PRD 시나리오 X / gen-2)
> 자산 ① (`test_K_Water/neo4j_client/`, 2026-03-07) 의 검증된 HyDE→임베딩→벡터 검색 파이프라인을
> K-AIR-meta-api 의 v0.6 RC 응답 계약(8 endpoint + `meta_version` + `X-Meta-Version` 헤더)으로
> 정렬한 신규 마이크로서비스.

## 1. 한 줄 정리

```
외부 AI ──[HTTP v0.6 body]──> robo-meta-api(8097) ──[Cypher / pgvector]──> robo-neo4j(7687) + robo-postgres(5432)
                                  │
                                  └ K-AIR-meta-api(8096, gen-1) 와 v0.6 RC 응답 동등
```

## 2. 빠른 시작 (로컬)

전제 — 호스트에 다음 컨테이너가 가동 중일 것:

- `robo-neo4j` (`neo4j:2025.11.2-community`, 7687) — 메타 그래프 (text_to_sql_table_vec_index ONLINE, 402 테이블)
- `robo-postgres` (`postgres:15-alpine`, 5432) — 원천 RDB `rwis` DB / `RWIS` 스키마

```bash
git clone <this-repo>
cd robo-meta-api
cp .env.example .env
# .env 에 OPENAI_API_KEY 채움 (HyDE 활성화) — 비워두면 keyword Cypher 폴백 모드
pip install -r requirements.txt
python -m app.main
# -> http://127.0.0.1:8097
```

회귀:

```bash
python tests/smoke_v06.py        # 8 endpoint 200 + meta_version=0.6
python scripts/compare_gen1_gen2.py  # gen-1 vs gen-2 Jaccard ≥ 0.95 (OPENAI 키 필요)
```

Docker:

```bash
docker compose up -d --build
docker logs -f robo-meta-api
curl http://127.0.0.1:8097/health
```

## 3. 디렉토리 구조

```
robo-meta-api/
├── app/
│   ├── main.py                          # FastAPI 부팅 + lifespan (Neo4j + 원천 PG)
│   ├── config.py                        # 통합 settings (Neo4j + 원천 PG + decision + exec)
│   ├── db.py                            # Neo4j 드라이버 + 원천 PG psycopg 풀
│   ├── schemas.py                       # K-AIR v0.6 RC 그대로 (차용)
│   ├── routers/
│   │   ├── decision.py                  # POST /data_decision
│   │   ├── meta.py                      # POST /meta/{batch,table,column,ref}
│   │   └── query_exec.py                # POST /query/execute (K-AIR 그대로)
│   ├── services/
│   │   ├── decision_service.py          # HyDE+벡터 → DecisionResponse 변환 + keyword Cypher 폴백
│   │   ├── meta_service.py              # /meta/* 4 endpoint Cypher
│   │   ├── subject_area.py              # K-AIR 차용 (분류 규칙)
│   │   ├── query_runner.py              # K-AIR 차용 (READ ONLY TX + 감사 JSONL)
│   │   ├── sql_guard.py                 # K-AIR 차용 (화이트/블랙리스트)
│   │   └── neo4j_client/                # 자산 ① 그대로 (HyDE, embedding, vector_search, db_probe)
│   └── rules/subject_area_rules.yaml    # K-AIR 차용 (분류 규칙 YAML)
├── tests/smoke_v06.py                   # VER-MN-04 (8 endpoint 회귀)
├── scripts/compare_gen1_gen2.py         # VER-MN-03 (gen-1 vs gen-2 동등성)
├── Dockerfile / docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

## 4. 8 endpoint × 응답 키 (v0.6 RC body)

| Method | Path | 응답 핵심 키 |
|---|---|---|
| GET | `/health` | `meta_version=0.6` + `X-Meta-Version: 0.6` 헤더 |
| POST | `/data_decision` | `target / secondary_targets / confidence / candidates[].matched_columns / join_groups / threshold_used` |
| POST | `/meta/batch` | `items[].db / schema_name / table_name` |
| POST | `/meta/table` | `table_info / columns / fk` (v0.6 풍부 필드는 빈 값 폴백) |
| POST | `/meta/column` | `column.{column_name, data_type, constraints, ...}` |
| POST | `/meta/ref` | `fk[].{column_name, ref_schema_name, ref_table_name, ref_column_name}` |
| POST | `/query/execute` | `status / sql_executed / columns / rows / row_count / truncated / elapsed_ms` (sql_guard 차단 시 400) |

## 5. 폴백 정책 (PRD §5 REQ-MN-F-05 + R-3)

- **OPENAI 미설정 / quota 초과**: HyDE 와 임베딩 skip → **keyword Cypher 폴백** (`MATCH (t:Table) WHERE toLower(name/desc/analyzed) CONTAINS kw`)
- **v0.6 RC 풍부 필드** (`lineage_brief`, `ontology_anchors`, `code_lookup`, `term_mapping`, `value_examples`, `join_groups`): 데이터 미적재이므로 `null` 또는 `[]` 폴백
- **`db` 라벨 폴백 체인**: `Schema.db || DataSource.engine || META_DB_LABEL`

## 6. 컴포넌트 출처

| 원천 | 차용 모듈 | 변경 |
|---|---|---|
| 자산 ① ([`test_K_Water/neo4j_client/`](https://github.com/LeeSeungWoo-stlogic/test_K_Water)) | `app/services/neo4j_client/*` (8 파일) | OPENAI_API_KEY 하드코딩만 제거, 나머지 그대로 |
| [`K-AIR-meta-api`](https://github.com/LeeSeungWoo-stlogic/K-AIR-meta-api) | `app/schemas.py`, `app/services/{sql_guard,query_runner,subject_area}.py`, `app/routers/query_exec.py`, `app/rules/subject_area_rules.yaml` | 그대로 (수정 0) |
| 신규 | `app/main.py`, `app/config.py`, `app/db.py`, `app/services/{decision_service,meta_service}.py`, `app/routers/{decision,meta}.py` | ~930 lines |

## 7. 검증 게이트 매핑 (PRD §6)

- **VER-MN-01** `/health` v0.6 헤더 → [tests/smoke_v06.py](tests/smoke_v06.py) `[1/8]`
- **VER-MN-02** smoke_v06 회귀 → 동일 스크립트 `[1~8/8]`
- **VER-MN-03** gen-1 vs gen-2 동등성 (Jaccard ≥ 0.95) → [scripts/compare_gen1_gen2.py](scripts/compare_gen1_gen2.py) (OPENAI 키 필요)
- **VER-MN-04** 8 endpoint 모두 200 → smoke_v06 PASS
- **VER-MN-05** 시크릿 push 0 → `.gitignore` + `git log -p --all -- '.env*'` empty

## 8. 운영 RUNBOOK (요약)

| 운영 항목 | 명령 / 점검 |
|---|---|
| 헬스 | `curl http://127.0.0.1:8097/health` → `meta_version=0.6` |
| 부팅 | `python -m app.main` (Windows ProactorEventLoop 자동 회피) |
| 종료 | `Ctrl+C` (lifespan 이 Neo4j 드라이버 + PG 풀 close) |
| 회귀 | `python tests/smoke_v06.py` (8/8 PASS = VER-MN-04) |
| 동등성 | `python scripts/compare_gen1_gen2.py` (avg Jaccard@10 ≥ 0.95) |
| 감사 로그 | `logs/query_audit.jsonl` JSONL append (rotate 별도 권고) |
| OPENAI 키 회수 | `.env` 의 `OPENAI_API_KEY` 만 갱신 후 재기동 |
| Neo4j 인덱스 누락 시 | 자산 ① `vector_search.py` 가 자동으로 `vector.similarity.cosine` 스캔 폴백 |

## 9. 알려진 제약 / 후속 트랙

- **R-1 자산 ① 키 leak**: `test_K_Water/neo4j_client/config.py` 의 OPENAI_API_KEY 가 git 추적 상태. **회수(rotate) 권고**.
- **R-2 `:Database` ≠ `:DataSource`**: PRD §2 다이어그램의 `:Database` 는 실 라벨이 `:DataSource` + `:Schema`. 본 레포의 Cypher 는 실 라벨로 작성.
- **R-3 `Schema.db=oracle` 표기**: 원천은 robo-postgres 이나 `:Schema.db` 가 `oracle`. `META_DB_LABEL` 환경변수로 운영 시 라벨 통제 가능.
- **R-4 `text_to_sql_is_valid=TRUE` 49건만 운영급**: 나머지 337건은 임베딩은 있으나 미검증.
- **R-6 db_probe**: `value_examples` 풍부 필드는 빈 값 폴백 (별도 트랙).
- **R-8 OPENAI quota**: 현재 자산 ①의 키가 quota 초과 — VER-MN-03 보류 상태.
- **gen-1 폐기 시점**: 추후 결정 (이중 운영 유지).

## 10. 라이선스

Private. K-water 내부 사용.
