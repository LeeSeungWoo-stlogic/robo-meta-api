# RWIS `data_process` decide 적용안

근거: [RWIS_DATA_PROCESS.md](RWIS_DATA_PROCESS.md).

> **상태 (2026-08-26):** 초안 당시에는 구현 전이었으나 `app/services/decision_postgres/data_process.py`와
> `/data_decision` `query_plan.aggregation.tags[]`가 반영되어 있다. 방향 문서로 읽고,
> 현재 동작은 README·코드를 따른다.

## 목표

태그가 묶인 뒤의 측정 조회·합계·평균에서, 오늘 엔진이 글자를 무시하고 내는 오답(눈금 `SUM`, 순시 합계, 혼합 `SUM`, 9·C 시간 `SUM`, F 일값 재합산)을 막는다. `/data_decision`과 `/t2sql`은 같은 decide를 쓰므로 계약을 한곳에서 만든다.

목록·엔티티 해소·grain 선택·결측일은 이번 범위가 아니다.

## 넣지 않는 것

- 카탈로그 `t2s_value_mappings`에 글자 한글표를 심지 않는다.
- 시드팩·원천 코드표 신설·검토 아티팩트 위조를 하지 않는다.
- GENERATE 프롬프트에 M=순시 같은 백과를 넣지 않는다.
- 코어에 `rditag_tb` / `rdd01dd_tb` 물리명을 넣지 않는다.

## 넣는 곳

`robo-meta-api` decide. 글자→처리 종류 표만 권역 별칭과 같이 닫힌 RWIS 지식이다. 별칭은 바인딩 전 용어 확장이고, 글자 해석은 바인딩 후 차원 컬럼 값을 읽는 일이다.

권장 위치:

- 글자 → 처리 종류: `app/services/decision_postgres/` 아래 닫힌 표 (예: `data_process.py`). 소스 식별이 RWIS 마트일 때만 쓴다.
- 읽기: `tagsn`이 확정된 뒤 서빙 태그 차원에서 그 `tagsn`의 `data_process` **값**만 조회. 계획 `required_columns`는 이름만 있어 글자 값이 없다. 팩트 스캔·값매핑 금지.
- 조합: `aggregation_contract`가 질문의 SUM/AVG/조회와 처리 종류를 짝 지운다. 지금은 `aggregate`/`extremum`만 계약을 채워 조회(lookup)가 빠진다. 목록 축이면 글자를 보지 않는다.
- 전달: `QueryPlan.aggregation`. 거부는 계약을 비우거나 `unresolved`만 넣지 않는다. GENERATE가 `SUM`을 발명한다. 거부·차분 칸을 계약에 남기고 GENERATE가 NO_SQL·차분만 따른다.

지금 `aggregation_contract`는 함수가 비면 `AVG`, 합계면 `SUM`이다. `data_process`를 읽지 않는다.

## 처리 종류 → SQL 동작

| 종류 | 글자 | 조회 | 합계·기간 | 평균 |
|---|---|---|---|---|
| 순시·상태 | M, Q | 그 입자 VAL | 만들지 않음 | 가능 (일 종가를 일평균으로 쓰지 않음은 grain 후속) |
| 대수평균(저장값) | P | VAL (이미 계산됨) | 만들지 않음 | 다시 계산하지 않음. 저장된 VAL 조회 |
| 누적 눈금 | A | VAL | 해당 입자 마지막 − 이전 구간. `SUM(VAL)` 금지 | 만들지 않음 |
| 구간 증분 | D | VAL | `SUM(VAL)` | 하지 않음 |
| 가동 | S | 1분은 0/1, 시간·일은 분 | 가동 분만. 유량처럼 여러 S를 한 SUM 하지 않음 | — |
| 속도→일총량 | F | 시간은 속도, 일은 이미 총량 | 일은 재합산 금지. 시간은 그날 총량이 필요하면 SUM(시간 VAL) | 시간 AVG는 속도 평균 |
| 일총량·시간누적 | 9(㎥), C, Z | 일은 VAL, 9·C 시간은 당일 누적 | 일은 SUM 가능. 시간 SUM 금지. Z는 시간 행 없음 | — |
| 가상 | I | 행 있으면 VAL | 만들지 않음 | — |
| 접점 | B, R | 행 있으면 VAL | 만들지 않음 | — |

9의 공급율·F의 %처럼 글자와 단위가 어긋나면 합계를 만들지 않는다. 글자가 섞인 한 질의의 합계는 만들지 않는다. 거부의 다음(글자별 재질의)은 1차에 넣지 않는다.

## 1차 작업

1. 닫힌 글자→종류 표와 RWIS 소스에서만 켜기.
2. 측정 질의에서 확정 `tagsn`의 `data_process` 읽기. 목록 축은 건너뛰기.
3. `aggregation_contract`가 종류와 질문을 조합. 거부면 `unresolved` 또는 계약을 비우고 사유를 남긴다. 차분(A)은 GENERATE가 발명하지 않게 계약에 남긴다. 필요하면 `PlanAggregation` 필드를 최소로 추가한다.
4. `/t2sql` 프롬프트는 계약만 SoT로 쓴다. 거부는 계약 칸이다. 계약을 비우지 않는다.
5. 테스트(실측 태그, 한 질의 패치 금지):
   - 136 어제 사용량 → 차분 23,568이지 눈금 SUM이 아님
   - 25326 어제 합계 → SQL 없음
   - 구천 어제 측정값 합계 → SQL 없음
   - 금강유역본부 사업장 목록 → 지금과 같이 목록, 글자 혼합으로 실패하지 않음
   - 1297 어제 합계 → 일 VAL 31이지 시간 재합산의 재합산이 아님
   - 10047 어제 시간 합계 → 시간 SUM 금지
   - 10047과 129274 어제 값 → 같은 일 VAL

## 1차에 넣지 않는 것

- 최신값을 시간 마지막 vs 일 종가로 고르는 grain
- `어제` 평균을 시간 AVG로 내릴지
- 혼합 합계를 글자별로 쪼개 다시 묻기
- 한글 「순시」→`M` 필터
- 플랫폼 CLASSIFY/PUBLISH/값매핑 변경
- 전체 compose 재기동. 적용 시 `robo-meta-api`만 재빌드
