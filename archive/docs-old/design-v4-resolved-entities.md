# robo-meta-api v4 — A안 entity resolution (D2)

> meta_version **0.7** | 포트 **8100** | v3 path 동일

## A안 요약

`POST /data_decision` 1회에 HyDE+벡터+join_groups + **resolved_entities**(코드 해소) 제공.

## v0.7 응답 필드

- `resolved_entities[]` — mention, table, name_column, code_column, values[]
- `suggested_probes[]` — 자동 probe 실패 시 SQL 템플릿
- `resolution_status` — complete | partial | skipped | failed

## 내부 step 7

1. HyDE entities + 한글 토큰 추출
2. Neo4j master/code 테이블 name 컬럼 후보
3. `batch_db_probe` (SOURCE PG)
4. code_column JOIN probe → values

## 선행 조건

K-AIR-meta-ingest Neo4j pipeline: Table.subject_area, text_to_sql_vector, fkTo alias.

RWIS code probe registry: `app/rules/rwis_code_probe.yaml` (7 코드 테이블).  
상세: [entity_resolution_code_probe_report.md](./entity_resolution_code_probe_report.md)

## v3 대비

| | v3 | v4 |
|--|----|----|
| meta_version | 0.6 | 0.7 |
| entity resolution | 없음 | A안 |
| FK 1단 | fkTo only | fkTo + FK_TO_COLUMN |
