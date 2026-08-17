# 외부 T2SQL ↔ robo-meta-api-v4 연동 규약 (D3)

## 호출 순서

1. **필수:** `POST /data_decision` `{ "query": "..." }`
2. `resolved_entities` 비어있고 `suggested_probes` 있음 → `POST /query/execute` 1회
3. **금지:** 동일 질문으로 `/data_decision` 재호출

## 필드 소비

| 필드 | 사용 |
|------|------|
| candidates | SQL FROM/JOIN 후보 |
| join_groups | JOIN 경로 (via=fk 우선) |
| resolved_entities | WHERE 코드값 (suj_code=316 등) |
| suggested_probes | execute용 probe SQL |

## 예시 (RWIS)

```json
{
  "meta_version": "0.7",
  "resolved_entities": [{
    "mention": "수지정수장",
    "table": "rdisaup_tb",
    "name_column": "suj_name",
    "code_column": "suj_code",
    "values": [{"code": "316", "label": "수지정수장", "confidence": 1.0}]
  }],
  "resolution_status": "complete"
}
```

외부 T2SQL: 위 힌트로 최종 SELECT 생성·실행.
