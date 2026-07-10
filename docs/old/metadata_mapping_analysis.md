# K-AIR Neo4j 메타데이터 구조 분석 및 robo-meta-api 연동 방안 보고서

본 보고서는 K-AIR Neo4j Enterprise 덤프 데이터를 복원하여 추출한 실제 메타데이터 스펙 [neo4j_extracted_metadata.json](file:///c:/Users/user/Desktop/Vibe_coding_list/K-AIR_gitea/neo4j_extracted_metadata.json)과 `robo-meta-api` v0.6 RC API 스펙 간의 연결 및 데이터 전처리 방안을 대조 분석한 결과입니다.

---

## 1. Neo4j 실제 메타데이터 구조 (As-Is)
덤프 복원 결과, 메타데이터는 다음과 같은 노드 및 관계 모델링을 통해 유지되고 있습니다.

- **노드 모델 및 속성 스펙**:
  - **DataSource**: `{name, engine, host, port, database, user, password}` (1건)
  - **Schema**: `{name, db}` (28건)
  - **Table**: `{name, db, schema, description, analyzed_description, text_to_sql_is_valid, ...}` (227건)
  - **Column**: `{name, type, nullable, primary_key, fqn, ...}` (1795건)
- **관계(Relationships) 모델**:
  - `(DataSource)-[:HAS_SCHEMA]->(Schema)`
  - `(Schema)-[:HAS_TABLE]->(Table)` 또는 `(Table)-[:belongsTo]->(Schema)`
  - `(Table)-[:HAS_COLUMN]->(Column)` 또는 `(Table)-[:hasColumn]->(Column)`
  - `(Column)-[:REFERENCES]->(Column)` 또는 `(Column)-[:fkTo]->(Column)` (외래키 관계)
  - `(Table)-[:fkToTable]->(Table)` (테이블 간 외래키 맵핑)

---

## 2. API 규격 대조 및 연결 방안 (To-Be)
`robo-meta-api`의 8개 엔드포인트 중 메타데이터를 반환하는 핵심 스펙과의 데이터 연결 매핑 룰을 아래와 같이 수립합니다.

### A. `/meta/table` (MetaTableResponse)
이 엔드포인트는 테이블 상세 스펙 및 컬럼 목록, FK 관계를 반환합니다.

| API 필드명 | Neo4j 속성/관계 매핑 룰 | 데이터 전처리 및 폴백 정책 |
|---|---|---|
| `table_info.db` | `t.db` 속성 매핑 | `t.db` 값이 실제 Postgres 환경임에도 `"oracle"`로 되어 있는 경우, 클라이언트 호환을 위해 그대로 보존하거나 `META_DB_LABEL` 환경변수로 치환 가능하도록 오버라이드 로직 적용. |
| `table_info.schema_name` | `t.schema` 속성 매핑 | 스키마 명칭 정제. |
| `table_info.table_name` | `t.name` 속성 매핑 | 테이블 명칭. |
| `table_info.table_comment` | `t.description` 또는 `t.analyzed_description` | 설명이 존재하지 않는 경우 `null` 폴백. |
| `table_info.pk_columns` | `(t)-[:HAS_COLUMN]->(c)` 관계 중 `c.primary_key`가 `true`이거나 `"Y"`인 컬럼들의 목록 | 필터링 및 리스트 추출 후 바인딩. |
| `table_info.datasource` | `(d:DataSource)` 노드의 정보 | `id` -> `d.name`, `dialect` -> `d.engine`, `domain`/`owner` -> `null` 폴백. |
| `table_info.lineage_brief` | `(t)-[writes/from/writes_to]->(t2)` 리니지 릴레이션십 | K-AIR-analyzer의 파싱 데이터가 미적재된 상태이므로 `{upstream: [], downstream: []}` 빈 배열 폴백 (R-6). |
| `columns[]` | `(t)-[:HAS_COLUMN]->(c)`를 통해 수집된 Column 노드 정보 리스트 | `column_name` -> `c.name`, `data_type` -> `c.type`, `is_null` -> `c.nullable` (boolean 변환). |
| `columns[].constraints` | `c.primary_key` 여부에 따라 지정 | `c.primary_key`가 `true`이면 `["PK"]` 지정. `Column` 간 `REFERENCES` 관계가 존재하면 `["FK"]` 추가. |
| `columns[].code_lookup` / `term_mapping` / `value_examples` | Neo4j Column 속성 매핑 | 데이터 미적재 상태이므로 `null` 또는 빈 값 (`[]`) 폴백 처리. |
| `fk[]` | `(c1:Column)-[:REFERENCES]->(c2:Column)` 관계 정보 수집 | `column_name` -> `c1.name`, `ref_schema_name`/`ref_table_name`/`ref_column_name` -> `c2` 노드의 정보 추출 후 목록화. |

### B. `/data_decision` (DecisionResponse)
자연어 질의를 수신해 가장 유사한 테이블 후보군을 추천합니다.

- **임베딩 / HyDE RAG 동작**:
  - `openai_api_key`가 존재할 시, `EmbeddingClient`를 사용하여 질의를 벡터화하고 Neo4j의 `text_to_sql_table_vec_index` 인덱스를 스캔해 후보군(`candidates`)을 가져옵니다.
  - 검색된 `Table` 노드 정보로부터 `DecisionCandidate` 구조로 변환:
    - `db` -> `t.db` (폴백 체인: `META_DB_LABEL` 사용 가능)
    - `schema_name` -> `t.schema`
    - `table_name` -> `t.name`
    - `score` -> 벡터 코사인 유사도 스코어
  - **폴백 정책**: OpenAI 키가 없거나 Quota를 초과하는 경우 `keyword Cypher 폴백 모드`를 활성화하여, `CONTAINS` 기반의 부분 일치 쿼리로 대체해 후보군을 강제 수집합니다.

---

## 3. 핵심 전처리 가이드라인 및 결론
1. **인코딩 및 데이터 정제**: Neo4j 속성 내 특수문자나 개행문자가 포함된 한국어 설명(`description`)은 JSON 직렬화 시 깨지지 않도록 적절히 escaping 처리합니다.
2. **Oracle/Postgres 불일치 완화**: 덤프 데이터의 `Table.db` 속성이 실제 DB 제품군인 Postgres와 달라 생기는 불일치는 **`META_DB_LABEL` 환경변수를 룩업 테이블 형태로 연동**하여 극복합니다.
3. **v0.6 RC 풍부 필드 안정성 보장**: 온톨로지 앵커, SQL 리니지 등 아직 그래프에 생성되지 않은 인과관계/RAG 메타데이터들은 모두 스키마에 정의된 기본값(`null` 또는 `[]`)으로 안전하게 폴백하게 하여 API 흐름이 정지되지 않도록 차단합니다.
