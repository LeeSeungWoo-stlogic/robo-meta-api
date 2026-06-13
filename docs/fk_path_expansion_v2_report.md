# K-AIR NLQ와 robo-meta-api 테이블/컬럼 선별 정합성 검증 완료 보고서

## 1. 정합성 검증 결과 요약
**네, K-AIR의 NLQ 과정과 `robo-meta-api`에서 동일한 테이블 및 컬럼 정보가 선별되고 있음이 최종 확인되었습니다.**

### 상세 대조 결과
1. **유효성 필터링 정합성 (Table Node)**:
   - Neo4j DB를 검증한 결과, 전체 227개 테이블 노드 중 오라클 스키마에 속한 **52개 테이블은 `text_to_sql_db_exists = false` 및 `text_to_sql_is_valid = false`**로 동일하게 무효(invalid) 마킹되어 있습니다.
   - 포스트그레스 스키마에 속한 **174개 테이블은 모두 `null`** 상태로 적재되어 있어, K-AIR의 `COALESCE(t.text_to_sql_is_valid, true) = true` 조건과 `robo-meta-api`의 `COALESCE(t.text_to_sql_db_exists, true) = true` 조건 하에서 **정합성 불일치 건수가 0건**으로 완벽하게 동일한 테이블 집합을 매칭하고 있습니다.
2. **컬럼 유효성 정합성 (Column Node)**:
   - 전체 1,795개 컬럼 노드의 유효성 플래그가 모두 `null`로 동일하게 채워져 있어 필터링 결과가 완벽하게 일치합니다.
3. **질의 선별 결과 대조**:
   - `"RDITAG 태그 마스터 테이블 컬럼"` 질의 시, 양쪽 모두 유효성 필터가 적용되어 오라클의 `RDITAG_TB` 등은 걸러지고, 실제 포스트그레스 스키마의 `rditag_tb` 및 `rditagunit_tb` 2개 후보 테이블이 정상 선별되었습니다.

---

## 2. 검증 과정 중 발견 및 조치 사항 (Bug Fix)
검증을 위한 API 테스트 과정에서 `/meta/table` 등 메타데이터 조회 엔드포인트들이 포스트그레스 테이블에 대해 **404(Table Not Found)** 에러를 발생시키는 버그를 발견하여 조치하였습니다.

* **원인**:
  - Neo4j에 오라클 메타데이터는 `(Table)-[:belongsTo]->(Schema)` 방향으로 관계가 맺어진 반면, 포스트그레스 메타데이터는 `(Schema)-[:HAS_TABLE]->(Table)` 방향으로 적재되어 있었습니다.
  - 기존 `meta_service.py` 쿼리는 `:belongsTo` 방향만 조회하여 174개의 포스트그레스 테이블 정보 조회가 불가능했습니다.
* **조치**:
  - [meta_service.py](file:///c:/Users/user/Desktop/Vibe_coding_list/K-AIR_gitea/robo-meta-api/app/services/meta_service.py)의 모든 관련 Cypher 조회를 양방향 및 결합 관계(`-[:belongsTo|HAS_TABLE]-`)로 수정하여 두 적재 형식을 모두 지원하도록 교정했습니다.
  - [smoke_v06.py](file:///c:/Users/user/Desktop/Vibe_coding_list/K-AIR_gitea/robo-meta-api/tests/smoke_v06.py)의 스모크 테스트 기본 대상을 유효 포스트그레스 테이블(`rwis.rditag_tb.tagsn`)로 업데이트하였습니다.

---

## 3. 최종 스모크 테스트 결과
컨테이너 재빌드 후 8개 전체 엔드포인트를 기동 테스트하여 아래와 같이 **모든 테스트가 통과(PASS)**했습니다.

```bash
[1/8] /health -> 200 meta=0.6 hdr=0.6
[2/8] /data_decision -> 200 target=collect cands=2 mode=internal_hyde+vector
[3/8] /meta/batch -> 200 total=226
[4/8] /meta/table rwis.rditag_tb -> 200 cols=43 fk=0
[5/8] /meta/column rwis.rditag_tb.tagsn -> 200 dtype=None
[6/8] /meta/ref rwis.rditag_tb -> 200 fk=0
[7/8] /query/execute SELECT -> 200 api_status=ok rows=3
[8/8] /query/execute DROP -> 400 (expect 400)

PASS - all 8 endpoints OK (VER-MN-04 gate)
```
- `/meta/batch` 조회 대상 수 역시 기존 오라클 전용 52개에서 **총 226개 (52 + 174)**로 정확하게 확장 및 추출됨을 확인했습니다.
- 실제 포스트그레스 테이블에 대한 쿼리 실행(`query/execute`)도 성공적으로 연동을 마쳤습니다.
