# robo-meta-api v4 소비처 연동 안내서 (Draft)

> 문서 상태: Draft  
> 기준일: 2026-07-07  
> 기준 서비스: `robo-meta-api v4` / `meta_version=0.7` / Docker 포트 `8100`  
> 대상 소비처: 외부 T2SQL, NLQ Agent, GenBI/SQL Agent, 메타데이터 조회 클라이언트

## 1. 목적

`robo-meta-api`는 T2SQL/NLQ 계열 소비처를 위해 두 가지 역할을 제공하는 API 서비스다.

1. 자연어 질의가 들어오면 HyDE, vector search, schema rule, entity resolution 등을 통해 관련 메타데이터를 찾고 정해진 포맷으로 반환한다.
2. T2SQL 서비스가 생성한 SELECT SQL을 `/query/execute`로 받아 실제 데이터베이스에 실행하고 결과를 반환한다.

초기 `/meta/*` API는 T2SQL 서비스 측 별도 vector DB 구축을 위한 메타데이터 덤프/조회 용도로 마련되었다. 그러나 T2SQL 서비스 측에서 별도 vector DB를 구성하지 않는 방향으로 변경되었으므로, `/meta/*`는 핵심 소비 경로에서 제외하거나 비활성화하는 방향으로 정리한다.

현재 서비스는 아직 완성 단계가 아니며, 특히 운영 배포 주소, SLA, 인증 정책, 일부 엔드포인트의 최종 역할은 확정 전이다. 다만 v0.7 기준으로 소비처가 사전에 연동을 준비할 수 있도록 현재 사용 가능한 계약과 주의사항을 정리한다.

## 2. 현재 서비스 요약

| 항목 | 현재 기준 |
|---|---|
| 서비스명 | `robo-meta-api v4` |
| 메타 버전 | `0.7` |
| 로컬/Docker 포트 | `8100` |
| 로컬 Base URL | `http://127.0.0.1:8100` |
| OpenAPI | `http://127.0.0.1:8100/docs` |
| Health Check | `GET /health` |
| 핵심 엔드포인트 | `POST /data_decision`, `POST /query/execute` |
| 주요 소비자 | T2SQL, NLQ Agent, SQL 생성 Agent |
| 주요 의존성 | `robo-neo4j:7687`, `robo-postgres:5432`, OpenAI API Key |

운영 또는 통합 환경의 정식 host는 별도 배포 확정 후 소비처에 공지해야 한다. 현재 문서의 URL 예시는 로컬/Docker 기준이다.

## 3. 소비처 관점의 역할

소비처는 이 API를 메타데이터 의사결정 서비스이자 SELECT SQL 실행 대행 서비스로 사용한다.

`POST /data_decision`은 SQL 생성 전에 사용할 메타데이터 후보를 제공한다. `POST /query/execute`는 T2SQL 서비스가 생성한 최종 SELECT SQL 또는 보강용 probe SQL을 실행한다. 현재 구현은 PostgreSQL 직접 실행을 기준으로 하며, air-swmm 편입 이후에는 MindsDB를 통해 대상 DB에 질의하는 방식으로 전환해야 한다.

`robo-meta-api`가 제공하는 핵심 정보는 다음과 같다.

| 응답 필드 | 소비처 사용 목적 |
|---|---|
| `candidates` | 자연어 질의와 관련 있는 테이블 후보. SQL의 `FROM` 후보로 사용 |
| `matched_columns` | 후보 테이블 내 관련 컬럼 힌트. `SELECT`, `WHERE`, `GROUP BY` 후보로 사용 |
| `join_groups` | 후보 테이블 간 JOIN 경로 힌트. `via=fk`를 최우선 신뢰 |
| `resolved_entities` | 자연어 표현을 코드값으로 해소한 결과. 예: `수지정수장` -> `SUJ_CODE=316` |
| `suggested_probes` | 자동 코드 해소 실패 시 1회 조회할 probe SQL |
| `resolution_status` | entity resolution 결과 상태. SQL 생성 분기 기준 |

## 4. 권장 호출 흐름

### 4.1 기본 흐름

1. 소비처는 사용자 자연어 질의를 수신한다.
2. `POST /data_decision`을 1회 호출한다.
3. `candidates`, `matched_columns`, `join_groups`를 이용해 SQL 대상 테이블과 JOIN 후보를 결정한다.
4. `resolved_entities`가 있으면 해당 값을 `WHERE` 조건 후보로 사용한다.
5. `resolution_status`가 `partial` 또는 `failed`이고 `suggested_probes`가 있으면 `/query/execute`를 호출해 코드 후보를 보강한다.
6. 소비처가 최종 SQL을 생성한다.
7. T2SQL 서비스가 최종 SELECT SQL을 `/query/execute`로 전달해 실행 결과를 받는다.

### 4.2 중요한 금지/주의 규칙

- 동일 질문에 대해 `/data_decision`을 반복 재호출하지 않는다.
- `/query/execute`는 최종 SELECT 실행 및 probe SQL 실행을 담당한다. 단, 쓰기/DDL/DML은 차단해야 한다.
- `/query`는 현재 stub 성격이며 실제 T2SQL 실행 API로 간주하지 않는다.
- `/meta/*`는 별도 vector DB 구축용 덤프/조회 API로 출발했으나, 현재 핵심 경로에서는 제외 또는 비활성화 검토 대상이다.
- Request body에는 `schema` 또는 `schema_name`을 넣지 않는다. 스키마 범위는 서버 환경변수로 제어된다.
- `candidates[].db`는 원천 PostgreSQL DB명과 항상 같다고 가정하지 않는다. SQL 생성 시에는 `schema_name`, `table_name`, `resolved_entities`를 우선 신뢰한다.

## 5. Endpoint 요약

| Method | Path | 소비처 사용 여부 | 설명 |
|---|---|---:|---|
| `GET` | `/health` | 권장 | 서비스 상태 확인 |
| `POST` | `/data_decision` | 필수 | 자연어 질의 기반 테이블 후보, JOIN 힌트, 코드 해소 결과 반환 |
| `POST` | `/meta/table` | 비권장 | 별도 vector DB 구축용 상세 메타 조회. 비활성화 검토 대상 |
| `POST` | `/meta/column` | 비권장 | 별도 vector DB 구축용 컬럼 메타 조회. 비활성화 검토 대상 |
| `POST` | `/meta/ref` | 비권장 | FK/참조 메타 조회. 비활성화 검토 대상 |
| `POST` | `/meta/fk` | 비권장 | `/meta/ref` alias. 비활성화 검토 대상 |
| `POST` | `/meta/batch` | 비권장 | 배치 기준 메타 덤프. 비활성화 검토 대상 |
| `POST` | `/query/execute` | 필수 | T2SQL 생성 SELECT 및 probe SQL 실행 |
| `POST` | `/query` | 비권장 | 현재 stub |

## 6. Health Check

요청:

```bash
curl.exe -s http://127.0.0.1:8100/health
```

예상 응답 예:

```json
{
  "meta_version": "0.7",
  "status": "ok",
  "neo4j_uri": "bolt://host.docker.internal:7687",
  "source_pg": "host.docker.internal:5432/rwis",
  "openai_enabled": true
}
```

소비처는 최소한 `status=ok`, `meta_version=0.7` 여부를 확인한 뒤 기능 호출을 수행하는 것을 권장한다.

## 7. 핵심 API: `POST /data_decision`

### 7.1 요청

```bash
curl.exe -s -X POST http://127.0.0.1:8100/data_decision ^
  -H "Content-Type: application/json; charset=utf-8" ^
  -d "{\"query\":\"2025년 9월 15일 수지정수장 계측값 현황 알려줘\",\"include_matched_columns\":true,\"column_top_m\":10,\"auto_resolve_entities\":true}"
```

요청 본문:

```json
{
  "query": "2025년 9월 15일 수지정수장 계측값 현황 알려줘",
  "include_matched_columns": true,
  "column_top_m": 10,
  "auto_resolve_entities": true
}
```

| 필드 | 필수 | 기본값 | 설명 |
|---|---:|---|---|
| `query` | 필수 | 없음 | 사용자 자연어 질의 |
| `include_matched_columns` | 선택 | `true` | 후보 테이블별 컬럼 매칭 포함 여부 |
| `column_top_m` | 선택 | 서버 설정 | 테이블별 컬럼 매칭 상한 |
| `auto_resolve_entities` | 선택 | `true` | 자연어 mention의 코드값 자동 해소 시도 |

### 7.2 응답에서 우선 소비할 필드

```json
{
  "meta_version": "0.7",
  "target": "source",
  "confidence": 0.5,
  "candidates": [],
  "join_groups": [],
  "resolved_entities": [
    {
      "mention": "수지정수장",
      "entity_type": "code",
      "schema_name": "rwis",
      "table": "RDISAUP_TB",
      "name_column": "SUJ_NAME",
      "code_column": "SUJ_CODE",
      "values": [
        {
          "code": "316",
          "label": "수지정수장",
          "confidence": 1.0
        }
      ],
      "source": "db_probe"
    }
  ],
  "suggested_probes": [],
  "resolution_status": "complete"
}
```

소비처 해석:

- `resolution_status=complete`: `resolved_entities`의 코드값을 SQL `WHERE` 조건 후보로 사용한다.
- `resolution_status=partial`: 해소된 값은 사용하되, 미해소 mention은 사용자 확인 또는 `suggested_probes` 1회 실행으로 보강한다.
- `resolution_status=skipped`: entity resolution이 수행되지 않았으므로 테이블/컬럼 후보만 사용한다.
- `resolution_status=failed`: PG probe 또는 의존성 문제 가능성이 있으므로 `suggested_probes`가 있을 때만 제한적으로 보강한다.

## 8. T2SQL 소비 규칙

T2SQL 소비처는 `/data_decision` 응답을 다음 우선순위로 사용한다.

1. `resolved_entities`: 자연어 mention의 코드값을 `WHERE` 조건으로 반영한다.
2. `candidates`: SQL 대상 테이블 후보를 선정한다.
3. `matched_columns`: 질의 의미와 가까운 컬럼을 `SELECT`, `WHERE`, `ORDER BY`, `GROUP BY` 후보로 사용한다.
4. `join_groups`: 2개 이상 테이블 사용 시 JOIN 경로 후보로 사용한다.
5. `suggested_probes`: 코드 해소 실패 시 보강 조회에 사용한다.

`join_groups[].bridges[].via`는 다음 순서로 신뢰한다.

| via | 권장 처리 |
|---|---|
| `fk` | 최우선. 자동 JOIN 후보로 사용 가능 |
| `ontology` | 업무 의미 기반 후보. 검증 후 사용 |
| `term` | 표준용어/동의어 기반 후보. 검증 후 사용 |
| `embedding` | 의미 유사도 기반 후보. 보조 힌트로 사용 |
| `convention` | 명명 규칙 기반 후보. 자동 JOIN 금지, 컬럼 타입 확인 권장 |

## 9. `/query/execute` 실행 정책

`/query/execute`는 T2SQL 서비스가 생성한 SELECT SQL을 실제 데이터베이스에 대행 실행하는 엔드포인트다.

허용되는 사용:

- T2SQL 서비스가 생성한 최종 SELECT SQL 실행
- `/data_decision` 응답의 `suggested_probes[].sql` 실행
- smoke 또는 내부 진단

비권장 사용:

- 반복적인 탐색 쿼리 실행
- 쓰기/DDL/DML 목적 사용

현재 구현은 PostgreSQL에 직접 실행하는 구조다. air-swmm 편입 이후에는 MindsDB를 통해 대상 DB로 질의하는 방식으로 backend를 교체해야 한다. `/query/execute`는 실행 API로 유지하되, SQL guard, 권한, 감사 로그, timeout, row limit 정책을 함께 적용해야 한다.

## 10. 빈 값 보장

v0.7 응답은 미적재 또는 미해소 상태에서도 다음 기본 형태를 유지한다.

| 필드 | 미적재/미해소 시 |
|---|---|
| `secondary_targets` | `[]` |
| `candidates[].matched_columns` | `[]` |
| `join_groups` | `[]` |
| `resolved_entities` | `[]` |
| `suggested_probes` | `[]` |
| `resolution_status` | `skipped` 또는 실패 상태 |

소비처는 필드 누락보다 빈 배열/`null`/`unknown`을 기준으로 방어적으로 처리한다.

## 11. 현재 미완성/주의사항

| 항목 | 현재 상태 |
|---|---|
| 운영 배포 URL | 미확정. 현재 문서는 로컬/Docker `8100` 기준 |
| 인증/인가 | 미정. 운영 연동 전 별도 정책 필요 |
| SLA/장애 대응 | 미정. 현재는 개발/검증 기준 |
| `/query` | stub |
| `/query/execute` | 현재 PostgreSQL 직접 실행. air-swmm 편입 시 MindsDB 실행 backend로 전환 필요 |
| `/meta/*` | 별도 vector DB 구축용으로 출발. 현재는 핵심 경로에서 제외 또는 비활성화 검토 |
| entity resolution | v0.7 A안. PG `db_probe`와 registry 적용 범위에 영향 받음 |
| OpenAI 의존 | `openai_enabled=false` 또는 외부 LLM 장애 시 품질 저하 가능 |
| 스키마 범위 | request가 아니라 서버 env(`DECISION_SCHEMA_ALLOWLIST`, `SOURCE_PG_SCHEMA`, `PG_SCHEMAS`)로 제어 |

## 12. 소비처 체크리스트

연동 전 확인:

- `GET /health`가 `status=ok`를 반환하는가?
- 응답 `meta_version`이 `0.7`인가?
- `/data_decision` 1회 호출로 `candidates`와 `resolution_status`가 반환되는가?
- T2SQL 쪽에서 `resolved_entities`를 `WHERE` 조건 후보로 사용할 수 있는가?
- `join_groups[].bridges[].via=convention`을 자동 JOIN하지 않도록 처리했는가?
- `/query/execute`가 SELECT only, timeout, row limit, 감사 로그 정책을 적용하는가?
- air-swmm 편입 시 MindsDB backend 전환 범위가 식별되어 있는가?
- `/meta/*` 비활성화 시 기존 소비처 영향이 없는가?
- 빈 배열, `null`, `unknown` 응답을 정상 케이스로 처리하는가?

## 13. 관련 문서

`robo-meta-api` 레포 기준:

- `docs/api_spec_v0.7.md`: v0.7 API 상세 명세
- `docs/old/external_t2sql_integration.md`: 기존 T2SQL 간단 연동 규약
- `docs/agent_join_hints.md`: `join_groups` 소비 규칙
- `docs/old/design-v4-resolved-entities.md`: entity resolution 설계
- `RUNBOOK.md`: 기동, smoke, 트러블슈팅

`K-water_docs` 기준:

- `REPORT_260701_DB_RAG_GraphDB_Metadata_ToBe.md`: GraphDB SoT 기반 메타데이터 To-Be
- `AIR_SWMM_Metadata_Platform_Issues.md`: 메타데이터 수집/증강/소비 구조 이슈

## 14. 소비처 공유용 한 줄 안내

T2SQL/NLQ 소비처는 `robo-meta-api v4`의 `POST /data_decision`을 먼저 호출해 테이블 후보, JOIN 힌트, 자연어 코드값 해소 결과를 받은 뒤 최종 SQL을 생성하고, `POST /query/execute`로 실행 결과를 받는다. 현재 실행 backend는 PostgreSQL 직접 연결이며, air-swmm 편입 이후 MindsDB 경유 실행으로 전환해야 한다.
