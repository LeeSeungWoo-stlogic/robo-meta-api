# Change log

robo-meta-api 업데이트 이력입니다. 서비스 설명·기능 안내는 [`README.md`](README.md)를 봅니다.

---

## 2026-08-27

### `/meta/catalog` 정본화

- catalog에 표/컬럼 `comment`·`description`·논리명·`subject_area`를 넣음
- 나가는 FK `references`에 `constraint_name`·`position`. 들어오는 FK는 `referenced_by`
- `/meta/table`·`/meta/column`은 catalog 문서를 표 하나·컬럼 하나로 자른 응답
- `/meta/batch`·`/meta/ref`는 catalog에서 파생. 별도 소스/스키마 API 없음

관련: `app/schemas.py` · `app/services/meta_postgres.py` · `app/services/metadata_repository/_catalog.py`

### `/query_execute` 타임아웃

MindsDB가 늦게 주는 오류 JSON을 받도록 HTTP 대기에 `EXEC_ERROR_RETURN_GRACE_S`(기본 30초)를 더한다. 오류 본문은 `db_error`. 요청 `timeout_s` 스키마 상한 600. 기본 60 / 최댓값 300은 `EXEC_DEFAULT_TIMEOUT_S` · `EXEC_MAX_TIMEOUT_S`.

관련: `app/services/query_runner_mindsdb.py` · `app/schemas.py` · `app/runtime_config.py`

### `/data_decision` 한글 기간

`최근`/`지난`+길이 단어(사흘·나흘·일주일·한달 등)와 위치 단어(익월·내일·이번주 등)를 KST 시계로 해석한다. 길이만 있으면 되묻는다. 표·컬럼명은 쓰지 않는다.

관련: `app/services/decision_postgres/period.py`

---

## 2026-08-26

### 문서 정본화

기동 절차가 Neo4j/`robo-postgres`/`.env.rwis-test.example`을 정본처럼 쓰던 드리프트를 고쳤다.

- `RUNBOOK.md`·`README.md`: Store YAML + `METADATA_PG_PASSWORD` + 네트워크 `kair-metadata-platform_control-plane`, 포트 **8100**
- health는 Store 소스 목록 조회. 설정만 확인이 아님
- smoke 기준에 `suggested_probes` 없음
- `docs/query_execute_backend_guide.md`·`docs/kair_robo_meta_api_graph_contract.md`는 현행 아님 배너 (8097/8099, `/query/execute`, Neo4j)

코드 변경 없음. 문서만.

## 2026-08-24

### `/data_decision` 응답 슬롯

- HTTP에서 `secondary_targets`, `confidence` 제거
- 후보·`matched_columns`의 `score` 제거 (0.0/1.0 잔여값)
- `query_plan.aggregation.tags[]` 추가: tagsn 바인딩 뒤 `data_process` / `apply` / `unit`
- `tags[].unit`은 표시값 `unit_desc`
- `tag_combine` 문자열은 `/t2sql` GENERATE용으로 유지
- 관련: `app/schemas.py` · `data_process.aggregation_tags` · `store_first.aggregation_contract`

### `/meta/catalog` 타입 표기 (이미 1.0 계약에 포함)

- `character varying(n)` → `varchar(n)`
- HyDE 전용 칸은 `/data_decision` HTTP에서 제외. `/t2sql`은 용어집 라우트를 내부에서 사용

관련 테스트: `tests/test_store_first.py` · `tests/test_meta_catalog.py` · `tests/test_dtype_consume.py`

---

## 2026-08-20

`/meta`가 Store에서 꺼내는 테이블·컬럼 사실을 `/data_decision` 후보와 같은 슬롯 규칙으로 채움. 질의 전용 블록은 `/meta`에 없음.

| 항목 | 내용 |
| --- | --- |
| **`/meta` 슬롯** | 논리명 → `table_name_kr`/`column_name_kr`. 원본 설명 → `table_comment`/`column_comment`. 분석 설명 → `description` |
| **`/meta` 보강** | `subject_area`, `table_type`, `default_date_column`, `value_examples`, 길이 포함 `data_type`, `unit`, `format_pattern`, `has_code` |
| **`/meta/catalog`** | 서빙 중 연결·표·컬럼 구조만. 논리명·설명·`subject_area` 없음 |
| **`/meta/batch`** | 표 키 + 논리명·설명·`subject_area` 요약 |
| **`/meta/ref`** | Store `t2s_fk_constraints`의 from 테이블 = 요청 테이블인 행만 |
| **list vs lookup** | 행이 많다고 list가 아님. 측정값·변화·추이는 lookup |
| **기간** | 측정값을 묻는데 기간이 없으면 SQL을 만들지 않음 |
| **동의어** | 용어 그룹 멤버는 맞은 그룹 안에서만 확장 |

---

## 2026-08-19

자연어 → Store 메타 교차 → 계획 → SQL → `/query_execute`가 본 경로. confirm 재판 생략.

| 항목 | 내용 |
| --- | --- |
| **기간** | 집계·극값은 기간이 없으면 SQL을 만들지 않음 |
| **범위 OR/제외** | 같은 컬럼 코드는 IN. `아닌`은 NOT IN |
| **팩트 필요** | 추이·변화, 기간+측정 항목이면 팩트 |
| **극값** | 제일 낮/적 → MIN, 높/많 → MAX |
| **측정점 식별** | 태그 카탈로그 SELECT는 PK·설명. `TAG_NAME` 미사용 |
| **SQL 식별자** | 컬럼 문자 리터럴을 백틱 식별자로 되돌림 |
| **검수 기동** | `docker compose -f docker-compose.meaning.yml up -d --build` (호스트 8101) |

---

## 2026-08-17

Store 승인 메타만으로 decide/T2SQL 조립. 질문 ID·물리 표명·TAGSN 하드코딩 없음.

- 기간을 팩트보다 먼저 파싱. 월+추이 → 일 팩트, 일+시계열 → 시간 팩트
- 카탈로그-only JOIN 드롭. JOIN은 팩트(없으면 매핑) 앵커만
- `/data_decision` meta_version 1.0. Store 스키마·값매핑 미변경
- 라이브 검사 산출(`t2sql_test/`, `_tmp_*`) Git 제외

---

## 2026-08-11

- `t2s_value_mappings` 공백 무시 + Hangul 단독 멘션 경계
- `measurement.metric`이 Store VM에 매칭될 때만 필수 필터 승격
- 승인 FK 1 hop으로 EQ 필터 전파
- RWIS 전용 강바인드 금지. SoT=Store VM·승인 FK

관련: `tests/test_filter_propagation.py`

---

## 2026-08-08

- `decision_postgres` · `metadata_repository` · legacy `decision_service` 패키지 분할
- KT 피드백 라이브 검증. 필드를 robo에 하드코딩하지 않음

---

## 2026-08-06

- `/semantic_decision` 410. `artifact_id` 410
- `POST /query/execute` → `POST /query_execute`. 구경로 410
- Store Serving KEEP 6표와 정합
- `tests/smoke_data_decision_only.py`
