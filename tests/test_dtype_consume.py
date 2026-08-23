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
                "name": "SUJ_CODE",
                "score": 0.4,
                "is_primary_key": False,
                "is_foreign_key": True,
                "dtype": "character",
                "metadata": {"character_maximum_length": 2},
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
    assert by_name["LOG_TIME"].pk_ordinal == 1
    assert by_name["SUJ_CODE"].data_type == "character(2)"
    assert by_name["TAG_VALU"].data_type == "numeric"
    assert by_name["TAG_VALU"].format_pattern is None


def test_candidate_prefers_datasource_database_name() -> None:
    candidate = _candidate(
        {
            "id": 1,
            "db": "rwis",
            "database_name": "rwis_prod",
            "source_name": "rwis_mart_view",
            "engine": "postgresql",
            "schema_name": "rwis_mart",
            "name": "vw_measure_day",
            "original_name": "vw_measure_day",
            "score": 0.8,
            "subject_area": "agg",
        },
        [],
        source="vector",
    )
    assert candidate.db == "rwis_prod"
    assert candidate.source_name == "rwis_mart_view"


def test_candidate_maps_character_varying_to_varchar() -> None:
    candidate = _candidate(
        {
            "id": 1,
            "db": "src",
            "source_name": "rwis_mart_view",
            "engine": "postgresql",
            "schema_name": "S",
            "name": "MEAS_TB",
            "original_name": "MEAS_TB",
            "score": 0.8,
            "subject_area": "agg",
        },
        [
            {
                "name": "suj_code",
                "score": 0.4,
                "dtype": "character varying",
                "metadata": {"data_type_with_length": "character varying(10)"},
            }
        ],
        source="vector",
    )
    assert candidate.source_name == "rwis_mart_view"
    assert candidate.db == "src"
    assert candidate.engine == "postgresql"
    assert candidate.matched_columns[0].data_type == "varchar(10)"


def test_candidate_infers_format_pattern_from_allowed_samples() -> None:
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
                "is_primary_key": False,
                "is_foreign_key": False,
                "dtype": "character",
                "metadata": {
                    "sample_values": ["202401011200", "202401021300"],
                },
            }
        ],
        source="vector",
    )
    assert candidate.matched_columns[0].format_pattern == "YYYYMMDDHHMI"


def test_generate_prompt_uses_plan_tables_not_candidate_types() -> None:
    assert "query_plan.required_tables의 table_name이 SQL 식별자" in GENERATE_PROMPT
    assert "없는 표를 창작하지 마라" in GENERATE_PROMPT
