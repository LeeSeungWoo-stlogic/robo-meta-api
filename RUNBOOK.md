# robo-meta-api RUNBOOK

> 운영 절차서 — gen-2 (PRD 시나리오 X) v0.6 RC body 정합 meta-api
> 대상 환경: Windows / Linux 동일. Windows 는 ProactorEventLoop 회피 정책 자동 적용.

## 1. 컨테이너 의존성 (호스트에 사전 가동 필요)

| 컨테이너 | 이미지 | 포트 | 역할 |
|---|---|---|---|
| `robo-neo4j` | `neo4j:2025.11.2-community` | 7474 / 7687 | 메타 그래프 (`:Table` 402, vector index `text_to_sql_table_vec_index` ONLINE) |
| `robo-postgres` | `postgres:15-alpine` | 5432 | 원천 RDB `rwis` / `RWIS` 스키마 (386 테이블) |
| `robo-meta-api` (gen-2) | `robo-meta-api:dev` | **8097** | 본 서비스 |
| `K-AIR-meta-api` (gen-1, 옵션) | 기존 | 8096 | 이중 운영 (PRD OPEN-3 = 유지) |

## 2. 기동

### 2.1 로컬 직접 부팅

```powershell
cd C:\Users\LSW\Documents\GitHub\robo-meta-api
copy .env.example .env
# .env 에서 OPENAI_API_KEY 채움 (없어도 keyword Cypher 폴백으로 동작)
python -m app.main
```

콘솔 마지막에 `Uvicorn running on http://127.0.0.1:8097` 가 보이면 정상.

### 2.2 Docker

```powershell
docker compose up -d --build
docker logs -f robo-meta-api
```

Docker 내부에서 호스트의 `robo-neo4j` / `robo-postgres` 접근은 `host.docker.internal` 로 매핑 (compose 의 `extra_hosts`).

## 3. 헬스체크

```powershell
curl.exe http://127.0.0.1:8097/health
# {"meta_version":"0.6","status":"ok","neo4j_uri":"bolt://127.0.0.1:7687","source_pg":"127.0.0.1:5432/rwis","openai_enabled":true}
curl.exe -i http://127.0.0.1:8097/health | findstr "X-Meta-Version"
# X-Meta-Version: 0.6
```

## 4. 회귀 / 검증

### 4.1 VER-MN-04 (8 endpoint 회귀)

```powershell
python tests\smoke_v06.py
```

기대: `PASS - all 8 endpoints OK (VER-MN-04 gate)`. 실패 시 다음 항을 봄.

### 4.2 VER-MN-03 (gen-1 vs gen-2 Jaccard ≥ 0.95)

전제: gen-1 (K-AIR-meta-api 8096) 가 떠 있고 동일 OPENAI 키로 양쪽 작동.

```powershell
python scripts\compare_gen1_gen2.py
# avg Jaccard @top10 = 0.97xx -> PASS
```

질의 fixture 는 `scripts/compare_queries.txt` (있을 때) 또는 스크립트 안의 `DEFAULT_QUERIES`.

## 5. 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| `Application startup complete` 후 `Psycopg cannot use ProactorEventLoop` | `python -m uvicorn ...` 로 띄우지 않았는지 확인. **반드시 `python -m app.main` 으로 기동** (main.py 가 정책 사전 설정 + `loop="asyncio"` 명시) |
| `/data_decision` 가 cands=0 + `mode=internal_hyde+vector`, `hyde_status` 에 429 | **OPENAI quota 초과** (R-8). keyword 폴백으로 자동 전환되어 cands 채워짐. 정상 |
| `/data_decision` 가 cands=0 + `mode=keyword_only` | OPENAI_API_KEY 미설정 + keyword 폴백도 0 hit. 질의를 좀 더 일반적으로 (예: "RDITAG 태그") |
| `/meta/table` 404 | `db` 인자 생략 가능. `schema_name` + `table_name` 만으로 조회 시도. 실제로 Neo4j 에 없는 테이블이면 정상 404 |
| `/query/execute` 가 db_error | `robo-postgres` 컨테이너 down 또는 `RWIS` 스키마 권한. `docker ps | findstr postgres` 로 확인 |
| Neo4j 인덱스 누락 | 자산 ① `vector_search.py` 가 `vector.similarity.cosine` 으로 자동 폴백 (느림). `SHOW INDEXES` 로 `text_to_sql_table_vec_index` ONLINE 확인 |

## 6. 시크릿 관리

| 항목 | 정책 |
|---|---|
| `.env` | `.gitignore` 가 차단. 절대 커밋 금지 |
| `.env.example` | 키 값은 빈 문자열 유지 |
| OPENAI 키 회수 시 | `.env` 의 `OPENAI_API_KEY` 만 갱신 후 `python -m app.main` 재기동 |
| 감사 로그 (`logs/query_audit.jsonl`) | 외부 AI 호출자 IP 와 SQL 본문 포함. 별도 rotate / 마스킹 정책 필요 |
| 키 leak 회수 (R-1) | 자산 ① (`C:\Users\LSW\Documents\GitHub\test_K_Water\neo4j_client\config.py`) Line 19 의 하드코딩 키 회수 필요 |

## 7. 백업 / 복구

robo-meta-api 자체는 stateless. 백업 대상 없음. 단,

- `logs/query_audit.jsonl` 감사 로그 — 외부 저장소로 주기 백업 권고 (별도 트랙)
- 의존하는 `robo-neo4j` / `robo-postgres` 백업은 각 컨테이너의 RUNBOOK 따름

## 8. 확장 / 후속 작업

| 항목 | 위치 |
|---|---|
| matched_columns 컬럼 벡터 검색 추가 (현재 anchor keyword 기반) | `app/services/decision_service.py:_attach_matched_columns` |
| v0.6 풍부 필드 채움 (lineage_brief, ontology_anchors, code_lookup, term_mapping, value_examples) | PRD §8 별도 트랙 |
| 50건 질의 fixture (VER-MN-03 본격) | `scripts/compare_queries.txt` 신규 생성 |
| Argus Catalog 합류 후 term_mapping 채움 | `app/services/meta_service.py:get_table` 의 columns 변환부 |
| OpenAPI 문서 export | `python -c "from app.main import app; import json; print(json.dumps(app.openapi(), indent=2))" > docs/openapi.json` |
