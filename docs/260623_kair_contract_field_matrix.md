# 260623 KAIR Contract & Field Mapping Matrix

## KAIR Contract Summary (Consumer View)

### Identity and Merge Keys
- Table key: `datasource + schema + name`
- Column key: `datasource + schema + table + name`
- Idempotent MERGE 및 결정적 IRI 규칙 유지

### Relationship Compatibility (must-read)
- Table-Schema: `belongsTo | HAS_TABLE`
- Table-Column: `hasColumn | HAS_COLUMN`
- Column-FK: `fkTo | FK_TO_COLUMN`
- Table-FK: `fkToTable | FK_TO_TABLE`

### Non-PG Honesty Rule
- 비-PG(MindsDB federation)에서 결손 메타(FK/PK/pg_comment/row count)는 날조 금지
- label_ko는 표준사전 기반만 허용, `pg_comment` fallback 금지

## Mapping: KAIR Graph → robo-meta-api Response

| API/필드 | KAIR 그래프 소스 | 소비 규칙 |
|---|---|---|
| `/data_decision.candidates[].schema_name` | `Table.schema` | 비어 있으면 빈 문자열 허용 |
| `/data_decision.candidates[].table_name` | `Table.name` | 원본명 보존 |
| `/data_decision.candidates[].db` | `Schema.db` 우선, 없으면 `DataSource.engine`, 없으면 `META_DB_LABEL` | R-3 폴백 체인 |
| `/data_decision.candidates[].table_comment` | `Table.description` | null 허용 |
| `/data_decision.candidates[].description` | `Table.analyzed_description` 우선, 없으면 `Table.description` | LLM 설명 우선 |
| `/data_decision.join_groups[].bridges[].via=fk` | `Column-[:fkTo|FK_TO_COLUMN]->Column` | 컬럼 FK 1단 연결 |
| `/data_decision.join_groups[].bridges[].via=ontology` | `Table-[rel]-Table` (`rel` in configured list) | 2단 연결 |
| `/meta/batch.items[]` | `Table-[:belongsTo|HAS_TABLE]-Schema` | `text_to_sql_db_exists=false` 제외 |
| `/meta/table.columns[]` | `Table-[:hasColumn|HAS_COLUMN]->Column` | 관계명 혼재 허용 |
| `/meta/table.fk[]` | `Column-[:fkTo|FK_TO_COLUMN]->Column` + 역추적 Table/Schema | 위치 순서 생성 |
| `/meta/ref`/`/meta/fk` | 위와 동일 | path alias만 다름 |

## Mapping Gaps to Fix in Code
- `HAS_COLUMN` 단일 고정 쿼리를 union 관계로 확장 필요
- `fkTo` 단일 고정 쿼리를 union 관계로 확장 필요
- `/meta/fk` path alias 추가 필요
- `/query` stub route 노출 정책 단일화 필요

## Source References
- KAIR contract/implementation:
  - `KAIR/backend/routers/physical_layer.py`
  - `KAIR/backend/ontology_studio/modules/db_source/mindsdb_introspect.py`
  - `KAIR/openspec/specs/physical-layer-auto-ingest/spec.md`
  - `KAIR/openspec/specs/data-curation/spec.md`
- robo-meta-api consumer:
  - `app/services/meta_service.py`
  - `app/services/neo4j_client/vector_search.py`
  - `app/services/decision_service.py`
  - `app/routers/meta.py`
  - `app/schemas.py`
