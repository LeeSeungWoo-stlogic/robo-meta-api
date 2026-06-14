# join_groups Bridge 소비 규칙 (AI Agent용)

robo-meta-api v3 `/data_decision` 응답의 `join_groups`를 NLQ/SQL Agent가 사용할 때 참고할 규칙.

## via 우선순위

1. `fk` (confidence 1.0) — 최우선
2. `ontology` (confidence ~0.8)
3. `convention` (confidence ≤ 0.5) — 참고용

## convention bridge 처리

### confidence 분기

| 조건 | confidence | Agent 행동 |
|------|------------|------------|
| dtype 일치 (`cast_recommended` 없음) | ~0.5 | 일반 equi-join |
| dtype 불일치 (`cast_recommended:true` in path) | ~0.35 | **CAST/`::type` 검토 필수** |
| confidence ≤ 0.5 전체 | — | 자동 join 금지, `/meta/column` 재조회 권장 |

### path 토큰 해석

- `shared_column:{name}` — JOIN 키 후보 컬럼명
- `dtype:{left}↔{right}` — 양쪽 raw dtype (예: `varchar↔numeric`)
- `cast_recommended:true` — 타입 불일치. JOIN 전 양쪽 cast 정렬 필요

### CAST 예시 (PostgreSQL)

path: `dtype:varchar↔numeric`, shared_column: `plant_cd`

```sql
-- 값 도메인에 따라 방향 선택
t1.plant_cd::numeric = t2.plant_cd
-- 또는
t1.plant_cd = t2.plant_cd::text
```

### dtype unknown

`dtype:unknown↔numeric` 등 — `/meta/column` API로 실제 dtype 확인 후 cast 결정.
