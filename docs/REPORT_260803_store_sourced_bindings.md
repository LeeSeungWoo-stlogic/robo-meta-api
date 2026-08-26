# REPORT 260803 — Store-sourced source binding

기준일: 2026-08. K-AIR-metadata-platform Store를 소스 실행 정본으로 고정.

> **이후 계약 (PLAN 260805):** 경로는 `/query_execute`. `execution_context`는 Optional(sql-only).
> 아래 To-Be의 “필수”·`/query/execute`는 이 보고 시점 문구이며 현행 스키마와 다르다.

## 요약

| 영역 | As-Is | To-Be |
|------|-------|-------|
| Runtime YAML | `execution.source_bindings` + `default_source_instance_id` | 소스 등록 키 **금지** (존재 시 로드 실패) |
| Binding 조립 | YAML에 하드코딩한 integration/catalog/schema | 요청 `source_instance_id` → Store `t2s_datasources` |
| 미게시/불일치 | legacy YAML fallback 가능 | `mindsdb_*` NULL·불일치 시 **fail-closed** |
| `/query/execute` | `execution_context` optional + YAML default | `execution_context` **필수**, YAML default 없음 |
| Client claim | integration/catalog/object를 신뢰하지 않음 | 동일 (Store allowlist로 재검증) |

## 주요 경로

- `app/runtime_config.py` — `_FORBIDDEN_EXECUTION_KEYS`
- `app/services/execution_context_resolver.py` — `binding_from_store_row`
- `app/schemas.py` — `QueryExecuteRequest.execution_context` required
- `config/runtime-settings.example.yaml` — bindings 블록 제거
- `tests/test_source_bindings.py` — Store resolve·YAML ban 회귀

## 운영 메모

- 로컬 `runtime-settings.*.local.yaml`에서 `source_bindings`를 제거해야 기동됩니다.
- Metadata Platform에서 PUBLISH/MINDSDB_PROVISION이 READY이고 Store에
  `mindsdb_integration`/`mindsdb_catalog`가 채워진 뒤에만 execute가 성공합니다.
- 소스 UUID를 robo YAML에 넣지 않습니다 (platform Store SoT).
