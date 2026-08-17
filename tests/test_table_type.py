from app.schemas import SchemaRoleRequirement
from app.services.decision_postgres.helpers import _candidate
from app.services.decision_postgres.roles import (
    backfill_empty_role_candidates,
    prepare_role_candidate_rows,
)
from app.services.decision_postgres.table_type import (
    allowed_table_types_for_role,
    list_table_type,
    table_type_allows_role,
)
from app.services.t2sql.llm import GENERATE_PROMPT


def test_list_table_type_mapping() -> None:
    assert list_table_type("raw") == "Raw"
    assert list_table_type("agg") == "Fact"
    assert list_table_type("code") == "Code"
    assert list_table_type("master") == "Dimension"
    assert list_table_type("link") is None
    assert list_table_type("hist") is None
    assert list_table_type("unknown") is None
    assert list_table_type(None) is None


def test_candidate_keeps_pipeline_area_and_maps_list_type() -> None:
    fact = _candidate(
        {
            "id": 1,
            "db": "src",
            "source_name": "SRC",
            "schema_name": "S",
            "name": "DAY_TB",
            "original_name": "DAY_TB",
            "score": 0.9,
            "subject_area": "agg",
        },
        [],
        source="vector",
    )
    assert fact.subject_area == "agg"
    assert fact.table_type == "Fact"

    bridge = _candidate(
        {
            "id": 2,
            "db": "src",
            "source_name": "SRC",
            "schema_name": "S",
            "name": "MAP_TB",
            "original_name": "MAP_TB",
            "score": 0.4,
            "subject_area": "link",
        },
        [],
        source="vector",
    )
    assert bridge.subject_area == "link"
    assert bridge.table_type is None


def test_generate_prompt_does_not_use_candidate_table_type_as_sot() -> None:
    assert "query_plan.required_tables의 table_name이 SQL 식별자" in GENERATE_PROMPT
    assert "candidates[].table_type은 쿼리 뼈대 힌트" not in GENERATE_PROMPT
    assert "근거가 부족하면 NO_SQL" not in GENERATE_PROMPT
    assert "반드시 SELECT를 출력하라" in GENERATE_PROMPT


def test_role_type_gate_fact_vs_master() -> None:
    assert allowed_table_types_for_role("부유물 농도 계측 팩트") == frozenset(
        {"Fact", "Raw"}
    )
    assert allowed_table_types_for_role("태그 마스터 태그 TAG") == frozenset(
        {"Dimension"}
    )
    assert allowed_table_types_for_role("사업장 마스터") == frozenset(
        {"Dimension", "Code"}
    )
    assert table_type_allows_role("계측 팩트", "agg") is True
    assert table_type_allows_role("계측 팩트", "raw") is True
    assert table_type_allows_role("계측 팩트", "master") is False
    assert table_type_allows_role("계측 팩트", "code") is False
    assert table_type_allows_role("계측 팩트", "link") is False
    assert table_type_allows_role("태그 마스터", "master") is True
    assert table_type_allows_role("태그 마스터", "code") is False
    assert table_type_allows_role("사업장 마스터", "code") is True


def test_prepare_role_rows_drops_dimension_from_fact_role() -> None:
    role = SchemaRoleRequirement(
        role="부유물 농도 계측 팩트",
        necessity="required",
        cardinality="many",
        search_terms=["부유물", "농도", "계측"],
    )
    master = {
        "id": 1,
        "original_name": "tag_entity",
        "description": "측정항목 태그 마스터",
        "subject_area": "master",
        "score": 0.95,
    }
    raw = {
        "id": 2,
        "original_name": "sensor_snapshot",
        "description": "태그 식별자별 계측 측정값과 상태를 로그 시각 기준으로 기록",
        "subject_area": "raw",
        "score": 0.4,
    }
    kept = prepare_role_candidate_rows(
        role,
        [master, raw],
        semantic_floor=0.55,
        min_score_ratio=0.2,
    )
    names = [item["original_name"] for item in kept]
    assert "tag_entity" not in names
    assert "sensor_snapshot" in names


def test_prepare_role_rows_tag_master_drops_code_dictionary() -> None:
    role = SchemaRoleRequirement(
        role="태그 마스터",
        necessity="required",
        cardinality="one",
        search_terms=["태그", "측정항목", "TAG"],
    )
    dimension = {
        "id": 1,
        "original_name": "tag_entity",
        "description": "측정항목 태그 마스터",
        "subject_area": "master",
        "score": 0.5,
    }
    code = {
        "id": 2,
        "original_name": "tag_unit_code",
        "description": "태그 단위 코드",
        "subject_area": "code",
        "score": 0.9,
    }
    kept = prepare_role_candidate_rows(
        role,
        [dimension, code],
        semantic_floor=0.55,
        min_score_ratio=0.2,
    )
    names = [item["original_name"] for item in kept]
    assert names == ["tag_entity"]


def test_backfill_fills_tag_master_from_pool_dimension() -> None:
    role = SchemaRoleRequirement(
        role="태그 마스터",
        necessity="required",
        cardinality="one",
        search_terms=["태그", "측정항목", "TAG"],
    )
    dimension = {
        "id": 11,
        "original_name": "tag_entity",
        "description": "측정항목 태그 마스터",
        "subject_area": "master",
        "score": 1.0,
    }
    filled = backfill_empty_role_candidates(
        {"태그 마스터": []},
        [role],
        [dimension],
        semantic_floor=0.55,
        min_score_ratio=0.2,
    )
    names = [item["original_name"] for item in filled["태그 마스터"]]
    assert names == ["tag_entity"]
