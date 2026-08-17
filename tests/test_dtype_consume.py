from app.services.decision_postgres.helpers import _candidate
from app.services.t2sql.llm import GENERATE_PROMPT


def test_candidate_maps_dtype_and_format_pattern() -> None:
    candidate = _candidate(
        {
            "id": 1,
            "db": "src",
            "source_name": "SRC",
            "schema_name": "S",
            "name": "MEAS_TB",
            "original_name": "MEAS_TB",
            "score": 0.8,
            "subject_area": "agg",
        },
        [
            {
                "name": "LOG_TIME",
                "score": 0.6,
                "is_primary_key": True,
                "is_foreign_key": False,
                "dtype": "character",
                "metadata": {"format_pattern": "YYYYMMDDHHMI"},
            },
            {
                "name": "TAG_VALU",
                "score": 0.5,
                "is_primary_key": False,
                "is_foreign_key": False,
                "dtype": "numeric",
                "metadata": {},
            },
        ],
        source="vector",
    )
    by_name = {item.column_name: item for item in candidate.matched_columns}
    assert by_name["LOG_TIME"].data_type == "character"
    assert by_name["LOG_TIME"].format_pattern == "YYYYMMDDHHMI"
    assert by_name["TAG_VALU"].data_type == "numeric"
    assert by_name["TAG_VALU"].format_pattern is None


def test_generate_prompt_uses_plan_tables_not_candidate_types() -> None:
    assert "query_plan.required_tables의 table_name이 SQL 식별자" in GENERATE_PROMPT
    assert "없는 표를 창작하지 마라" in GENERATE_PROMPT
