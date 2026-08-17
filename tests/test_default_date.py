from app.services.decision_postgres.default_date import default_date_column
from app.services.decision_postgres.helpers import _candidate
from app.services.t2sql.llm import GENERATE_PROMPT


def test_prefers_primary_key_date_column() -> None:
    name = default_date_column(
        [
            {
                "name": "NOTE",
                "dtype": "text",
                "is_primary_key": False,
                "metadata": {},
            },
            {
                "name": "LOG_TIME",
                "dtype": "character",
                "is_primary_key": True,
                "metadata": {"format_pattern": "YYYYMMDDHHMI"},
            },
            {
                "name": "CRDT",
                "dtype": "character",
                "is_primary_key": False,
                "metadata": {"format_pattern": "YYYYMMDDHH"},
            },
        ]
    )
    assert name == "LOG_TIME"


def test_first_date_like_when_no_pk() -> None:
    name = default_date_column(
        [
            {
                "name": "CRDT",
                "dtype": "character",
                "is_primary_key": False,
                "metadata": {"format_pattern": "YYYYMMDD"},
            },
            {
                "name": "UPDT",
                "dtype": "date",
                "is_primary_key": False,
                "metadata": {},
            },
        ]
    )
    assert name == "CRDT"


def test_none_when_no_date_evidence() -> None:
    assert (
        default_date_column(
            [
                {
                    "name": "NAME",
                    "dtype": "character varying",
                    "is_primary_key": True,
                    "metadata": {},
                }
            ]
        )
        is None
    )


def test_candidate_maps_default_date_column() -> None:
    candidate = _candidate(
        {
            "id": 1,
            "db": "src",
            "source_name": "SRC",
            "schema_name": "S",
            "name": "FACT_TB",
            "original_name": "FACT_TB",
            "score": 0.8,
            "subject_area": "agg",
        },
        [
            {
                "name": "LOG_TIME",
                "score": 0.4,
                "is_primary_key": True,
                "is_foreign_key": False,
                "dtype": "character",
                "metadata": {"format_pattern": "YYYYMMDDHH"},
            }
        ],
        source="vector",
    )
    assert candidate.default_date_column == "LOG_TIME"


def test_generate_prompt_uses_resolved_period_only() -> None:
    assert "기간은 query_plan.filters의 resolved 기간만 써라" in GENERATE_PROMPT
    assert "날짜 컬럼을 창작하지 마라" in GENERATE_PROMPT


def test_generate_prompt_periodless_uses_max_not_omit() -> None:
    assert "MAX" in GENERATE_PROMPT
    assert "최신" in GENERATE_PROMPT
    assert "WITH/CTE를 쓰지 마라" in GENERATE_PROMPT
    assert "기간이 없거나 unresolved면 날짜 조건을 생략" not in GENERATE_PROMPT
