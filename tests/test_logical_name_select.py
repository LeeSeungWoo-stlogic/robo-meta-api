from pathlib import Path

from app.services.t2sql.llm import GENERATE_PROMPT

_REPO = Path(__file__).resolve().parents[1]
_SELECT = "NULLIF(t.metadata->>'logical_name', '') AS logical_name"


def test_search_tables_selects_logical_name() -> None:
    source = (_REPO / "app/services/metadata_repository/_search.py").read_text(
        encoding="utf-8"
    )
    assert _SELECT in source


def test_fetch_tables_by_ids_selects_logical_name() -> None:
    source = (_REPO / "app/services/metadata_repository/_joins.py").read_text(
        encoding="utf-8"
    )
    assert _SELECT in source


def test_find_value_mapping_code_columns_uses_verified_mappings() -> None:
    source = (_REPO / "app/services/metadata_repository/_search.py").read_text(
        encoding="utf-8"
    )
    assert "async def find_value_mapping_code_columns" in source
    assert "FROM t2s_value_mappings vm" in source
    assert "vm.verified = true" in source


def test_find_value_mappings_uses_activation_join() -> None:
    source = (_REPO / "app/services/metadata_repository/_search.py").read_text(
        encoding="utf-8"
    )
    assert "JOIN t2s_snapshot_activations a" in source
    assert "source_instance_id: str | None = None" in source


def test_generate_prompt_uses_plan_table_names() -> None:
    assert "query_plan.required_tables의 table_name이 SQL 식별자" in GENERATE_PROMPT
    assert "없는 표를 창작하지 마라" in GENERATE_PROMPT


def test_generate_prompt_uses_resolved_filters_as_where_sot() -> None:
    assert "query_plan.filters 중 resolution_status=resolved" in GENERATE_PROMPT
    assert "코드를 창작하지 마라" in GENERATE_PROMPT
    assert "코드 테이블을 JOIN하지 마라" in GENERATE_PROMPT
    assert "resolution_status=unresolved 인 필터의 value를 WHERE 리터럴로 쓰지 마라" in GENERATE_PROMPT


def test_generate_prompt_aggregates_value_column_not_join_keys() -> None:
    assert "AVG/SUM/MAX/MIN의 대상은 value_column뿐" in GENERATE_PROMPT
    assert "JOIN 키·식별 컬럼에 집계 함수를 걸지 마라" in GENERATE_PROMPT


def test_generate_sql_payload_includes_query_plan() -> None:
    source = (_REPO / "app/services/t2sql/engine.py").read_text(encoding="utf-8")
    assert '"query_plan": plan.model_dump(by_alias=True) if plan else None' in source
