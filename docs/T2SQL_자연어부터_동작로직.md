# POST /t2sql 동작 로직

`robo-meta-api`가 자연어를 받아 읽기 전용 SQL을 확정하거나 실패할 때까지의 경로다.

`/t2sql`은 업무 결과 행을 돌려주지 않는다. 확정 SQL을 만들고, 그 SQL을 검사하려고 MindsDB에 검증 SELECT를 보낸다. 결과 집합은 클라이언트가 반환 SQL로 `POST /query_execute`를 호출해 얻는다.

조회 문자열 목록의 코드 인자 이름은 `needles`다. 본문에서는 조회 문자열이라고 한다.

본문 값 예시는 아래 두 질문이다. 의미 LLM 칸은 호출마다 문장이 달라질 수 있다. 표에 적은 값은 **그 칸이 이렇게 잡힌 경우**의 이후 단계 값이다.

| 기호 | 질문 | 성격 |
|---|---|---|
| Q1 | `금강권역 정수장 목록` | 범위 + 목록. 팩트·측정 없음 |
| Q2 | `화성정수장 평균 탁도` | 사업장 + 측정 + 집계. 팩트 필요 |

요청 본문에 필드를 생략하면 스키마 기본값이다. Q1·Q2 예시 열은 생략 시를 기준으로 한다.

---

## 1. 요청

`app/main.py`가 `t2sql.router`를 장착한다.  
`POST /t2sql` → `run_t2sql(get_metadata_repository(), req)`.

### 1.1 T2SqlRequest

| 필드 | 스키마 기본 | 클라이언트가 안 내면 | Q1·Q2 생략 시 값 | 하는 일 |
|---|---|---|---|---|
| `query` | 필수 | 422 | `금강권역 정수장 목록` / `화성정수장 평균 탁도` | 앞뒤 공백 제거. 공백만이면 거부 |
| `include_matched_columns` | `true` | `true` | `true` | 응답 `used_metadata.candidates[].matched_columns`를 채울지. SQL 생성에는 영향 없음 |
| `table_limit` | `null` | runtime `decision.table_top_k`(YAML 예: 10) | `null` → 실제 cap 10 | 계획·후보에 올릴 표 수 상한(1–50) |
| `auto_resolve_entities` | `true` | `true` | `true` | 값매핑을 `resolved_entities`에 심을지. `include_resolved_entities`와 동일 의미. `false`여도 `QueryPlan.filters`·probe·confirm은 돈다 |

파이프라인 벽시계는 요청 필드가 아니다. YAML `t2sql.total_timeout_seconds`(스키마 기본 60, 허용 1–120)만 쓴다. `/query_execute.timeout_s`는 MindsDB statement timeout이며 별개다.

#### `include_matched_columns` 켜고/끄고

엔진은 `decide()`에 항상 `include_matched_columns=True`를 넘긴다. 끄면 **응답만** 비운다.

| 값 | decide 내부 | 응답 `used_metadata.candidates[].matched_columns` | SQL |
|---|---|---|---|
| `true` | 선정 표 승인 컬럼을 후보에 채움 | 확정 SQL에 나온 표의 그 컬럼 목록 | 동일 |
| `false` | 위와 같음 | `[]` | 동일 |

Q1 `{"query":"금강권역 정수장 목록"}` (기본 true): 확정 SQL에 정수장 마스터가 있으면 그 표의 승인 컬럼이 `matched_columns`에 남는다.  
같은 질문에 `"include_matched_columns": false`: SQL·필터·코드는 같고 `matched_columns`만 빈 배열이다.

`matched_columns[].has_code`는 승인 값사전의 `code_column`이면 `Y`, 아니면 `N`이다. 코드 표라는 이유만으로 명칭·정렬 컬럼에 `Y`를 주지 않는다.

#### `table_limit` 기본이 비어 보이는 이유

요청 스키마에 숫자 10을 박지 않는다. 환경 YAML이 상한이다. 클라이언트가 안 내면 서버가 YAML을 쓴다. 질의창에서 다시 묻지 않는다.

`column_top_m`은 `T2SqlRequest`에 없다. 레거시 `/data_decision` `DecisionRequest`에만 있다.

| 필드 | 요청 생략 | Serving `/t2sql`에서 실제 |
|---|---|---|
| `table_limit` | `decision.table_top_k` | 후보 슬라이스 `ordered_tables[:effective_top_k]` |

Q1에서 `table_limit`를 1로 내면 후보·계획 표가 1개로 잘릴 수 있다. 생략하면 YAML 10이다.

#### `auto_resolve_entities` 켜고/끄고

값매핑 → `QueryPlan.filters`는 이 플래그와 무관하다. 바뀌는 것은 엔티티 배열이다.

| 값 | `resolved_entities` | `QueryPlan.filters` | probe | confirm |
|---|---|---|---|---|
| `true` | 값매핑 행을 엔티티로 시드. mention은 `natural_value` | 코드 EQ/IN 유지 | 코드가 이미 있으면 그 엔티티는 probe 안 함 | 실행 |
| `false` | `[]` | 동일하게 코드 필터 | 엔티티가 없어 엔티티 probe 대상이 없음 | 실행 |

Q1 기본(`true`): 범위 코드가 902로 확정되면 엔티티 mention `금강유역본부(충청)` 등, `values[].code=902`. 코드가 있으므로 probe는 그 엔티티를 치지 않는다.  
Q1 `"auto_resolve_entities": false`: 계획 필터 `BNB_CODE = '902'`는 남고, `used_metadata.resolved_entities`는 비어 있다.

### 1.2 T2SqlRuntime

`model`과 `base_url`이 둘 다 있어야 SQL 생성을 시도한다. 모델은 env `T2SQL_LLM_MODEL`이 YAML보다 우선. `base_url`은 YAML, 없으면 의미 분해 base, 없으면 embedding base.

| 한도 | 기본 | Q1·Q2에서 |
|---|---|---|
| `max_probe_steps` | 4 | 엔티티 코드가 있으면 0회 |
| `probe_timeout_seconds` | 8 | statement timeout 상한에 클립 |
| `probe_row_limit` | 20 | probe SQL `LIMIT` |
| `generate_timeout_seconds` | 30 | 생성 LLM 단계 상한 |
| `max_generate_retries` | 1 | 검증 실패 시 재시도 1회 |
| `validate_max_rows` | 5 | 검증 SELECT `LIMIT` |
| `total_timeout_seconds` | 60 | 파이프라인 벽시계. 일찍 끝나면 남은 초를 채우지 않음 |

---

## 2. 오케스트레이션 시작

파일: `app/services/t2sql/engine.py` `run_t2sql`

| 순서 | 동작 | Q1 생략 요청일 때 |
|---|---|---|
| 1 | `generation_id` 발급 | UUID |
| 2 | t2sql 미설정이면 `failed` / `UPSTREAM_UNAVAILABLE` | 모델이 있으면 통과 |
| 3 | `total_timeout_seconds`로 벽시계. LLM·probe·검증은 남은 시간을 나눈다. statement timeout은 `min(남은시간, 단계상한, execution.maximum_timeout_seconds)` | 상한 60초. 보통 그보다 짧게 끝남 |
| 4 | 이미 만료면 `TIMEOUT` | 시작 직후는 해당 없음 |
| 5 | `decide()` 호출 (`app/services/decision_postgres/decide.py`) | Q1 원문 전달 |

이후 엔진은 질문 원문을 다시 해석하지 않는다. 생성 가능 여부는 계획 칸만 본다.

---

## 3. 의미 분해

파일: `app/services/query_analysis.py` `QueryAnalyzer.analyze`

스토어를 읽지 않는다. 질문만 LLM에 넣는다.

다음이면 `status=degraded`, `meaning_status=failed`로 끝난다. 슬롯은 비어 있다.

| 실패 조건 | 이후 |
|---|---|
| `decision.hyde_enabled` 꺼짐 | 슬롯 공란, 표 찾기는 질문 토큰 |
| embedding API key 없음 | 동일 |
| 넘긴 timeout 1초 미만 | 동일 |
| LLM timeout·예외·JSON 비객체 | 동일 |

성공 시 OpenAI 호환 chat.

| 설정 | 값 |
|---|---|
| 모델 | `decision.hyde_model` |
| base_url | `analysis_base_url`, 없으면 embedding base_url |
| `max_retries` | 0 |
| `max_completion_tokens` | 1200 |
| `response_format` | `json_object` |
| `analysis_reasoning_effort` | true이면 `reasoning_effort=low` |
| 시스템 | 스토어·물리표·컬럼·코드값을 쓰지 말 것. 질문만으로 확보할 것을 적을 것 |
| 사용자 메시지 | 질문 원문만 |

출력 칸과 예시 질문 값:

| 칸 | 의미 | Q1 예 | Q2 예 |
|---|---|---|---|
| `goal` | 한 줄 목표 | 금강권역 정수장 목록 | 화성정수장 탁도 평균 |
| `procedure` | `lookup` / `list` / `aggregate` / `extremum` | `list` | `aggregate` |
| `procedure_why` | 절차 이유 | 목록 요청 | 평균 요청 |
| `metric` | 측정 표현. 없으면 빈 문자열 | `""` | `탁도` |
| `target` | 범위. 답의 축 이름을 반복하지 않음 | `금강권역` | `화성정수장` |
| `period` | 기간 원문. 없으면 빈 문자열. 없다고 latest로 바꾸지 않음 | `""` | `""` |
| `meaning_roles` | 역할, necessity, cardinality, 짧은 `search_terms` | 범위=금강권역, 축=정수장 | 사업장=화성정수장, 측정=탁도 |
| `primary_outputs` | 답의 축 이름 | `["정수장"]` | 측정·평균에 따라 `["탁도"]` 또는 공란 |
| `answer_must_include` | 답에 있어야 할 표현 | 정수장 명칭 | 평균 탁도 |
| `meaning_status` | complete / partial | `complete` | `complete` |

procedure 값:

| 값 | 뜻 |
|---|---|
| lookup | 특정 대상의 값 |
| list | 대상 목록 |
| aggregate | 합·평균·건수 |
| extremum | 가장 높/낮/많/적 |

`_sanitize` 후처리:

| 규칙 | 효과 |
|---|---|
| `db.schema.table` 형태 | 그 칸을 비움 |
| procedure가 허용 집합이 아님 | 공란 |
| 물리명을 지웠거나 procedure가 빔 | `meaning_status`를 partial 또는 failed로 내림 |
| 역할·search_terms의 물리명 제거 | 컬럼처럼 보이는 역할(이름/명칭/시각 등, 마스터·이력 제외)은 부모 역할 search_terms로 흡수하고 역할 목록에서 뺌 |
| `_dual_write` | `goal` → `intent`, `metric` → `measurement.metric`, `meaning_roles` → `schema_roles`. `target`을 `filter_requirements`의 `범위 대상`으로 넣음. `/t2sql` 본 경로는 `target` / `metric` / `procedure` / `primary_outputs`를 씀 |
| extremum | aggregation 기본 MAX |
| list/lookup | aggregation을 비움 |
| `entities_include` | 답 축·target과 같은 항목을 뺌 |

표 검색은 임베딩이 아니다. 이후 단계는 조회 문자열 동등·접두로 스토어를 읽는다.

---

## 4. 슬롯 → 조회 문자열

파일: `app/services/meaning_slots.py`

| 함수 | 규칙 | Q1 | Q2 |
|---|---|---|---|
| `compact` | 공백 제거 + casefold | `금강권역정수장목록` | `화성정수장평균탁도` |
| 조회 표면 | 공백 제거 길이 2–24, 공백 2개 이상 금지, 정의 절 표지(`하는`, `하여`, `해당하는`, `또는`, `그리고`) 금지, 물리명 금지 | 통과하는 짧은 칸만 | 동일 |
| `axis_mention` | 끝이 `목록` / `현황` / `리스트`이면 접미를 뗌 | `정수장` (`정수장 목록`이면) | 해당 없으면 원문 |
| `range_target_needle(target, primary_outputs)` | target 공란 → 공란. 답 축과 같으면 범위 아님. 답 축 줄기로 끝나면 앞부분만. 아니면 target 전체가 짧은 표면이면 그대로 | `금강권역` | `화성정수장` |
| 범위 조회 목록 | 위 범위 원문. 답 축이거나 `meaning_status=failed`이면 빈 목록 | `["금강권역"]` | `["화성정수장"]` |
| 측정 조회 목록 | `metric`. 답 축이면 빈 목록 | `[]` | `["탁도"]` |
| 표 찾기 목록 | 답 축 + 범위 원문 + 범위·측정 합집합 + `meaning_roles.search_terms`. 의미 실패면 질문에서 한글/라틴 덩어리를 자르고 조사·불용어·숫자를 뺌 | `정수장`, `금강권역`, … | `화성정수장`, `탁도`, … |

범위 원문이 있고 답 축이 아니면, 카탈로그 조회에만 `expand_region_hq_aliases`를 붙인다. `권역` / `유역` / `지역본부` / `권역본부` / `유역본부` 또는 `본부`가 있으면 그룹 전체와 `사례+동의어` 표기를 만든다. compact 길이 4 미만은 버린다. 값 코드 조회가 아니라 표 이름 검색용이다.

| 질문 | 카탈로그 별칭 확장 |
|---|---|
| Q1 | `권역`이 있어 본부 그룹·`금강`+접미 표기가 카탈로그 조회에 붙을 수 있음 |
| Q2 | 사업장 표지. 본부 그룹 확장은 `권역`/`유역`/`본부`가 질문에 있을 때 |

---

## 5. 용어집·값 코드·표 이름

### 5.1 용어집

`find_glossary_routes`는 필터·측정·답 축 줄기를 넣는다. 범위 원문은 넣지 않는다. 승인 표준용어·표준단어 동등. LLM 표시·가중 컬럼 판별용이다.

값매핑 extra SoT는 `find_synonym_groups(필터+측정)`의 매칭 용어 그룹 멤버다. routes 전역 LIMIT에 extra를 묶지 않는다. 범위 슬롯은 접미 그룹 peel만 쓴다.

`resolved_entities.source`는 그 멘션이 어떻게 값매핑에 들어왔는지를 표시한다. 코드 자체는 값사전이다.

| `source` | 조건 | 예 |
|---|---|---|
| `glossary` | `match_type=alias_prefix`, 또는 compact 멘션이 용어 그룹·glossary route 표면에 있음, 또는 범위 peel로 원문과 사전 라벨이 다름 | `NTU`→탁도, `탁도` 표준용어, `금강유역`→금강유역본부 |
| `value_examples` | 값사전 라벨을 질문 원문과 바로 맞춘 경우 | `충주정수장` 사업장 코드 |
| `db_probe` | Serving decide가 아닌 레거시 probe 경로 | T2SQL 본선 미사용 |

| 규칙 | Q1 | Q2 |
|---|---|---|
| 측정 바늘이 용어 그룹 멤버에 있을 때만 그 그룹 동의어를 값매핑 extra로 씀 | 측정 조회 없음 → extra 없음 | `탁도`가 용어 그룹에 있으면 그 멤버만 extra |
| 범위에는 용어 extra를 넣지 않음 | `금강권역` extra 없음 | `화성정수장` extra 없음 |

### 5.2 값매핑 조회

파일: `app/services/metadata_repository/_search.py` `find_value_mappings`

조건: Serving 활성 스냅샷, `verified=true`, 표 `text_to_sql_is_valid`, `review_status=approved`.

SQL:

| 조건 | 내용 |
|---|---|
| 조회 문자열 | 공백 제거 `natural_value` 동등, 또는 괄호 `(` / `（` 앞 머리 동등 |
| extra가 있으면 | `natural_value` compact가 extra로 시작하거나, `code_value` 동등 |

파이썬 `_select_mentioned_mappings`:

| 순서 | 동작 |
|---|---|
| 1 | 조회 문자열 동등·머리 적중을 남긴다 |
| 2 | extra 중 trusted만 접두 또는 코드값 역방향으로 남긴다. 별량 사전에서 짧은 말이 긴 라벨의 접두이고 나머지가 질문에 있으면 버린다 |
| 3 | 동등 적중끼리, compact한 전체 라벨(괄호 유지)에서 짧은 쪽이 긴 쪽의 접두면 짧은 행을 삭제한다 |
| 4 | 역방향 적중이 이미 남은 라벨의 접두면 삭제한다 |

동등 판정은 괄호를 벗긴 머리를 쓴다. 짧은 라벨 삭제는 괄호를 벗기지 않는다. 머리가 같은 두 코드가 있으면, 괄호 별칭이 붙은 쪽이 더 긴 문자열이라 다른 코드 행이 삭제될 수 있다.

### 5.3 범위 코드

`project_range_mappings` (`decide.py`, `aliases.py`).

유형만인 슬롯(접미를 벗기면 사례가 빈 문자열)은 코드 조회를 하지 않고 `범위 코드 미결합`도 넣지 않는다.

사례가 있으면:

| 단 | 동작 | Q1 | Q2 |
|---|---|---|---|
| 1단 | 범위 원문만 동등. extra 없음. distinct `code_value`가 1개면 확정, `matched_mention`은 범위 원문. 행은 있는데 코드가 0개이거나 2개 이상이면 2단을 하지 않고 미결합. 0건이면 2단 | `금강권역` 동등 0건이면 2단 | `화성정수장`이 값사전에 있으면 1단에서 코드 1개로 확정될 수 있음 |
| 2단 접미 | compact에서 닫힌 유형 접미를 가장 긴 것 하나, 한 번 벗김. 없으면 사례 원문 × 모든 유형 그룹 접미 곱. 글자를 더 깎지 않음 | `권역` → 사례 `금강`, 그룹 본부 | `정수장` → 사례 `화성`, 그룹 사업장. `충주`는 접미 없음 → `충주정수장`/`충주사업장` |
| 2단 조회 | 사례 × 그 그룹 접미 곱만 동등. extra·접두 없음. 적중은 그룹 사전 표지만 | `금강지역본부`, `금강유역본부`, `금강권역본부`, `금강유역`, `금강권역` | `화성사업장`, `화성정수장` |
| 확정 | 접미가 있으면 코드 있는 행을 남김. 접미가 없으면 distinct `code_value`가 1개일 때만 확정 | `금강권역`은 접미 경로 | `충주`→`충주정수장` 코드 1개 |

본부 접미: `지역본부`, `유역본부`, `권역본부`, `유역`, `권역`.  
사업장 접미: `정수장`, `사업장`.  
본부 사전: 논리명에 지역본부/유역본부/권역본부, 또는 본부이면서 사업장/정수장이 아닌 행. 사업장 행은 본부 사전에 넣지 않는다.

### 5.4 측정 코드

| 조건 | 동작 | Q1 | Q2 |
|---|---|---|---|
| 의미 실패 | 값매핑 안 함 | 해당 없으면 진행 | 동일 |
| 1차 | 측정 조회 문자열 동등. extra·trusted는 용어집 동의어. extra 적중은 **바늘 동등이 난 같은 표**에서만 남김 | 측정 조회 없음 | `탁도` 동등. `NTU` extra는 변량표에만 |
| 실패 시 | 측정 문자열을 앞에서부터 길이 2까지 잘라 extra에 넣고 한 번 더. trusted는 용어집뿐이라, 접두 extra로 SQL에 걸려도 trusted가 아니면 버림 | 해당 없음 | `탁` 등 접두 extra 재조회 가능 |
| 범위 | 이 재조회 없음 | — | — |

### 5.5 표 이름

`find_catalog_by_mentions`: Serving 표 전체와 승인 컬럼을 읽는다.

| 순서 | 채택 조건 |
|---|---|
| 1 | 논리명이 조회 문자열과 동등·머리 |
| 2 | 아니면 논리명·설명·분석설명·컬럼 설명·`column_name_kr`·컬럼 logical_name에 동등 또는 접두 |

소스가 하나로 가려지면 그 소스 행만 남긴다.

측정 매핑은 같은 `matched_mention`에 라벨이 여러 개이면, 컬럼이 하나이고 토큰 길이 3 이상이 아닌 한 애매 그룹으로 미룬다. 범위에서 확정한 행은 이 분할 앞에 붙인다.

시드 표 id = 값매핑 `table_id` ∪ 카탈로그 표 id. 그룹 차원(`본부별` 등)은 카탈로그에서 따로 모은다.  
측정 라벨이 아직 안 덮이면 측정만 다시 조회한다. 범위 원문은 넣지 않는다.

| 시드·그룹 차원 | 결과 |
|---|---|
| 둘 다 없고 범위 미결합 | 계획에 `범위 코드 미결합`만 넣고 종료 |
| 둘 다 없음 | `맞는 메타데이터가 없다` |

| 질문 | 시드 예 |
|---|---|
| Q1 | 정수장 마스터 + 범위 코드가 붙은 표(예: 본부 코드 컬럼이 있는 사업장 표) |
| Q2 | 화성정수장 값매핑 표 + 탁도 측정이 있는 팩트 후보 |

---

## 6. FK 확장과 팩트 선정

시드에서 `fk_max_hops`만큼 이웃 표를 가져온다.

기간은 `analysis.period`, 없으면 질문 원문이다. `parse_korean_period`, `resolve_time_grain`. `grain_override`가 있으면 그걸 쓴다.

| 입도 규칙 | 효과 | Q1 | Q2 |
|---|---|---|---|
| 주간 | 월 입도 표를 팩트 풀에서 뺌 | 기간 없음 | 기간 없음 |
| 연 구간이고 답이 월로 좁혀짐 | 월 표만 남김 | 해당 없음 | 해당 없음 |

`query_requests_fact`:

| procedure | 팩트 |
|---|---|
| `list` / `lookup` / 공란 | 고르지 않음 |
| `aggregate` / `extremum` | 고름 |
| 그 외 | `metric`이 있을 때만 고름 |

Q1 `list` → 팩트 없음. Q2 `aggregate` → 팩트 선정.

`pick_fact_tables`:

| 단계 | 내용 |
|---|---|
| 풀 | subject_area가 Fact 또는 Raw인 표만 |
| 입도 힌트 있음 | 논리명·설명에 힌트 단어가 있으면 그 부분집합. 1개면 확정, 2개 이상이면 `팩트 표 미선정`으로 집합을 넘김. 0개면 더 고운 입도로 내려가고, 그래도 없으면 `팩트 입도를 스토어 설명과 맞출 수 없음` |
| 힌트 없음 | 질문과 표 설명의 월/일/시간 점수, Fact 선호, 일 입도 선호 |
| 목록이 2개 | 값매핑과 조인 가능 여부, 주간 축소, 시계, 유일 유형, 일 입도로 줄임. 그래도 2개면 팩트를 비우고 `팩트 표 미선정` |

선정 id = 값매핑 표 ∪ 그룹 차원 ∪ 팩트.  
`list`이고 팩트가 없으면 별량/측정항목/태그 Code 표를 넣는다. 위치 그룹 표도 넣는다. 태그 마스터는 컬럼 조회용으로 id만 보강한다.

같은 조회 표면이 부모 코드표와 자식 복합표에 있으면, 측정 허브에 있는 코드 컬럼을 고른다(`project_code_mappings_to_hub`).

카탈로그만 있고 값매핑·팩트와 조인이 안 되면 그 표를 뺀다. 고르지 않은 팩트도 뺀다.  
승인 FK로 JOIN 경로를 만들고, 경유 표는 시계열 식별 표로 승격할 수 있다.

---

## 7. 필터와 QueryPlan

`mapping_filters`:

| 규칙 | Q1 | Q2 |
|---|---|---|
| 의미 실패면 필터 없음 | 의미 성공이면 진행 | 동일 |
| `X별` / `X들` 유형 언급, `X목록` 또는 `X이나 Y` 목록 축, 답 축과 같은 표면, 변위 사업장 행은 코드 필터에서 뺌. list여도 `target`과 같다고 범위 필터를 버리지 않음 | `금강권역`은 답 축 `정수장`이 아니므로 범위 코드 필터 유지 | `화성정수장` 코드 EQ |
| 컬럼별로 코드 집합. 1개면 EQ, 2개 이상이면 IN | 확정 코드 1개면 EQ `'902'` | 사업장 코드 1개면 EQ |
| `meaning` | `코드매핑:{natural_value}` | 예: `코드매핑:금강유역본부(충청)` | 예: `코드매핑:화성정수장` |

이어서 태그 설명 LIKE 등 `measure_point_label_filters`.  
팩트가 있으면 스토어 날짜 컬럼으로 기간 필터. 없으면 기간 원문만 컬럼 없이 둔다. 질문만 주간 언급이면 unresolved 기간 필터.  
승인 FK 한 홉으로 resolved EQ/IN을 계획·브리지 표에 복사한다.

표마다 `required_columns`: JOIN ON 컬럼, 필터 컬럼, 팩트 측정·시각, Dimension/Code/태그 마스터의 명칭·코드. 감사 날짜와 관계 코드는 제외한다. 태그 마스터는 상위 코드·주소를 정체성 컬럼에서 뺀다.

`QueryPlan`:

| 칸 | 내용 | Q1 예 | Q2 예 |
|---|---|---|---|
| `required_tables` | 쓸 표 | 정수장(사업장) 마스터 | 사업장 마스터 + 탁도 팩트 |
| `bridge_tables` | JOIN 경유 | 있으면 경로상 표 | 있으면 경로상 표 |
| `join_paths` | 승인 ON | FK 경로 | 마스터–팩트 ON |
| `filters` | resolved 코드 등 | 본부/권역 코드 컬럼 EQ `'902'` | 사업장 코드 EQ, 측정 코드, AVG 대상 컬럼 |
| `unresolved_requirements` | 미결합 | 코드 1개면 비어 있음 | 팩트 미선정이면 문구 있음 |
| `time_role` | `extremum`만 extremum, 아니면 none | `none` | `none` |
| `answer_axis` | `primary_outputs` | `["정수장"]` | `["탁도"]` 또는 공란 |
| completeness | 표도 필터도 없으면 failed. unresolved가 있거나 의미가 degraded면 partial. 아니면 complete | `complete` | 팩트·코드가 모이면 `complete` |

`resolved_entities`의 `mention`은 값사전 `natural_value`, `matched_mention`은 질문에 맞은 표면이다. `table`은 스토어 표기. `source`는 §5.1. `auto_resolve_entities=false`면 빈 배열.  
`execution_context` 바인딩이 실패해도 계획은 반환한다.  
`DecisionResponse`가 엔진으로 돌아간다. 이 시점에도 SQL은 없다.

---

## 8. 생성 전 게이트

파일: `engine.py`

| 순서 | 조건 | 코드 | Q1 | Q2 |
|---|---|---|---|---|
| 1 | `맞는 메타데이터가 없다` | `NO_METADATA` | 시드가 있으면 통과 | 동일 |
| 2 | 팩트가 필요한데 팩트 미선정 또는 입도 미결합 | `PLAN_INCOMPLETE` | list라 해당 없음 | 탁도 팩트를 못 고르면 실패 |
| 3 | `범위 코드 미결합` | `PLAN_INCOMPLETE`. 표가 있어도 생성하지 않음 | 902 확정이면 통과 | 화성 코드 확정이면 통과 |
| 4 | 팩트가 필요 없으면 팩트 unresolved 문구만 계획에서 지움 | — | 해당되면 지움 | 팩트 필요라 유지 |
| 5 | `required_tables`도 resolved 필터 값도 없음 | `PLAN_INCOMPLETE` | 표·902가 있으면 통과 | 표·코드가 있으면 통과 |
| 6 | 이종 소스 JOIN | `CROSS_DB` | 단일 소스면 통과 | 동일 |
| 7 | `source_instance_id` 없음 또는 Store datasource 없음 | `PLAN_INCOMPLETE` | 바인딩 성공 시 통과 | 동일 |
| 8 | 실행 바인딩 실패 | `PLAN_INCOMPLETE` | 실패 시 여기 | 동일 |

---

## 9. Probe

파일: `app/services/t2sql/probe.py`

표와 명칭 컬럼이 있고 `values[].code`가 전부 비어 있는 엔티티만 친다. 값매핑으로 코드가 있으면 probe하지 않는다.

| 항목 | 내용 | Q1 (코드 902 확정) | Q2 (사업장 코드 확정) |
|---|---|---|---|
| allowlist | subject_area가 master/code인 Serving 객체와 엔티티 표 | 마스터 표 | 마스터 표 |
| SQL | `SourceName.Schema.Table`에서 명칭 `LIKE '%mention%'`, 코드 컬럼이 있으면 SELECT에 포함. `LIMIT probe_row_limit` | 코드가 있어 스킵 | 코드가 있어 스킵 |
| allowlist 밖 | 건너뜀 | — | — |
| caller | `t2sql_probe` | — | — |
| timeout | 전체 `TIMEOUT` | — | — |
| 그 외 실패 | 카운트만 올리고 계속 | — | — |
| 성공 행 | confirm·generate 입력의 `probe_rows` | 비어 있을 수 있음 | 비어 있을 수 있음 |

---

## 10. Confirm

계획 필터 value에 없는 엔티티 코드는 버린다.

confirm LLM을 건너뛰는 조건:

| 조건 | Q1 | Q2 |
|---|---|---|
| 범위에 사례가 있으면, 기간이 아닌 resolved 필터 값이 있을 때 | `금강` 사례 + `'902'` → 건너뜀 | `화성` 사례 + 사업장 코드 → 건너뜀 |
| 그렇지 않으면 resolved 값이 하나라도 있을 때 | — | — |

건너뛰면 `{accept: true, missing: []}`.

아니면 confirm LLM. `max_retries=0`, `max_completion_tokens=400`, JSON. resolved 필터를 missing에 넣지 말 것, 기간을 창작하지 말 것. 입력은 질문, 분석, 계획, 용어집, 엔티티, probe 행.

`reconcile_confirm`:

| missing 종류 | 삭제 조건 |
|---|---|
| 기간 | 항상 삭제 |
| 코드·facility | 기간이 아닌 resolved 코드 필터가 있을 때만 삭제 |
| 집계 | analysis aggregation이 있으면 삭제 |
| 팩트 | 계획이 팩트 미선정이면 삭제 |
| 역할 | 역할 이름 동등이거나 칸에 `마스터`가 있을 때만 삭제 |

`accept=false` → `ENTITY_UNRESOLVED`. confirm LLM 예외 → `UPSTREAM_UNAVAILABLE`.

---

## 11. SQL 생성과 검증

generate LLM. `max_completion_tokens=4096`. SELECT 또는 WITH…SELECT 한 문장만.

| 요지 | Q1 | Q2 |
|---|---|---|
| `SourceName.Schema.Table` 백틱 3단 | 정수장 마스터 3단 | 팩트·마스터 3단 |
| `required_tables`만 사용 | 계획 표만 | 계획 표만 |
| resolved 필터 value는 따옴표 문자 리터럴 | `'902'` | 사업장 코드 리터럴 |
| `join_paths.conditions`는 ON 정본 | 경로가 있으면 | 마스터–팩트 ON |
| `SELECT *` 금지. `required_columns`만 | 명칭·코드 | 측정·AVG·키 |
| 목록에 LIMIT를 넣지 않음 | LIMIT 없음 | 집계라 행 수 정책은 생성기 지침에 따름 |
| 팩트 미선정이고 측정이 필요하면 `NO_SQL: 팩트 표 미선정` | 해당 없음 | 팩트 실패 시 |
| 목록은 차원 명칭·코드 | 정수장 이름·코드 | — |
| `time_role=latest`일 때만 시각 MAX 서브쿼리 | `none` | `none` |
| extremum이고 답 축이 있으면 축 GROUP BY 후 극값. 전역 LIMIT 1 금지 | 해당 없음 | 해당 없음 |
| `table_notes`에서 표를 다시 고르지 않음 | — | — |

입력: 질문, 분석, 계획, 용어집, 엔티티, probe 행, source_name, table_notes.

재시도 횟수: `1 + max_generate_retries`. 매 시도:

| 순서 | 동작 | Q1 성공 시 |
|---|---|---|
| 1 | 마크다운 펜스 제거. 식별자 백틱은 유지 | — |
| 2 | 계획 resolved 숫자 코드에 따옴표가 없으면 씌움 | `'902'` |
| 3 | `NO_SQL`로 시작하면 `GENERATION_FAILED` | 해당 없음 |
| 4 | 가드: Serving 전체 `allowed_object_refs` allowlist. WHERE가 없고 표가 master/code allowlist 밖이면 거부. SQL에 양쪽 표가 있는데 승인 ON 컬럼 이름이 SQL에 없으면 거부. 계획에 없는 표면 거부 | 마스터+코드 WHERE면 통과 |
| 5 | `LIMIT validate_max_rows`를 붙인 뒤 caller `t2sql_validate`로 실행. `/query_execute`가 아님 | 검증만 5행 |
| 6 | timeout → `TIMEOUT` | — |
| 7 | rejected / db_error → 재시도 | — |
| 8 | ok → `generated`. SQL은 공개 수식으로 정규화. `used_metadata`는 SQL에 나온 표만. 계획·분석은 유지 | `sql_status=generated` |

검증이 0행이고 질문이 명시 `월별`이 아닌 추정 월이면, 일 입도로 `decide`를 한 번 더 하고 생성을 다시 한다. 그때 성공하면 `sql_reason`에 `월 팩트 0건으로 일 팩트를 재조회했습니다`. Q1·Q2는 기간이 없으면 이 분기를 타지 않는다.

전부 실패하면 `validation_failed` / `GUARD_REJECTED` 또는 `GENERATION_FAILED`. 마지막 초안이 있으면 `sql`에 실을 수 있다.

MindsDB 실행 입구에서 SELECT 외 문장은 한 번 더 차단한다.

Q1 라이브 확정 SQL 요지: 정수장 목록 SELECT, 범위 컬럼 EQ `'902'` (`금강유역본부(충청)`). 공개 SQL에는 검증 LIMIT가 없다.

---

## 12. 응답

`T2SqlResponse`:

| 필드 | 내용 | Q1 성공 예 | Q2 성공 예 |
|---|---|---|---|
| `meta_version` | 계약 버전 | `1.0` | `1.0` |
| `generation_id` | 호출 UUID | UUID | UUID |
| `sql` | 성공 시 확정문. 실패면 비거나 가드에 걸린 초안 | 정수장 목록 + `'902'` | 화성 코드 + AVG(탁도) |
| `sql_status` | generated / failed / validation_failed | `generated` | `generated` |
| `sql_reason` / `sql_reason_code` | `NO_METADATA`, `PLAN_INCOMPLETE`, `ENTITY_UNRESOLVED`, `CROSS_DB`, `GUARD_REJECTED`, `GENERATION_FAILED`, `TIMEOUT`, `UPSTREAM_UNAVAILABLE` | 성공이면 null | 성공이면 null |
| `elapsed_ms` | 벽시계 전부가 아님. 실제 소요 | 60초보다 짧음 | 동일 |
| `used_metadata` | SQL에 나온 표·계획·분석 | 정수장 표, 필터 902 | 팩트·마스터 |
| `probe_summary` | probe SQL·건수 | 코드 확정이면 probe 없음 | 동일 |

---

## 순서

```
POST /t2sql {query}
 → 모델·YAML 벽시계
 → 의미 LLM (스토어 없음)
 → 범위 / 측정 / 표 조회 문자열
 → 용어집
 → 범위 코드: 원문 동등 → (0건이면) 유형접미 1회 × 동의어 동등
      → 짧은 라벨 삭제 → 코드 1개만 확정
 → 측정 코드: 동등 → (실패 시) 접두 extra 재조회
 → 표 이름 검색 → FK 확장 → 팩트 선정
 → 코드 필터 → FK 한 홉 전파 → JOIN ON → required_columns → QueryPlan
 → 메타없음 / 팩트미선정 / 범위미결합 / 표없음 / 이종DB / 소스없음
 → 코드 없는 엔티티만 probe
 → confirm LLM (코드가 있어도 생략하지 않음)
 → SQL LLM
 → allowlist · 팩트 WHERE · 승인 ON · 계획 표
 → MindsDB 검증 SELECT
 → (0행·추정 입도면) 더 고운 입도 재계획 1회
 → generated SQL
```

---

## 소스

| 단계 | 파일 |
|---|---|
| 라우터 | `app/routers/t2sql.py`, `app/main.py` |
| 오케스트레이션 | `app/services/t2sql/engine.py` |
| 의미 분해 | `app/services/query_analysis.py` |
| 슬롯 | `app/services/meaning_slots.py` |
| 계획 | `app/services/decision_postgres/decide.py` |
| 유형 접미·사전 표지 | `app/services/decision_postgres/aliases.py` |
| 값매핑·카탈로그 | `app/services/metadata_repository/_search.py` |
| 시드·팩트·필터 | `app/services/decision_postgres/store_first.py` |
| FK 필터 전파 | `app/services/decision_postgres/filters.py` |
| 입도 | `app/services/decision_postgres/grain.py` |
| confirm | `app/services/t2sql/confirm.py` |
| confirm·generate LLM | `app/services/t2sql/llm.py` |
| probe | `app/services/t2sql/probe.py` |
| JOIN ON 가드 | `app/services/t2sql/join_on.py` |
| used_metadata | `app/services/t2sql/used_meta.py` |
| 계약 | `app/schemas.py` |
| 런타임 | `app/runtime_config.py` |
