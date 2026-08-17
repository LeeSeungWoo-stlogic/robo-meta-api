from app.services.decision_postgres.helpers import _candidate
from app.services.t2sql.llm import GENERATE_PROMPT


def test_candidate_maps_sample_values() -> None:
    candidate = _candidate(
        {
            "id": 1,
            "db": "src",
            "source_name": "SRC",
            "schema_name": "S",
            "name": "CODE_TB",
            "original_name": "CODE_TB",
            "score": 0.8,
            "subject_area": "code",
        },
        [
            {
                "name": "STATUS_CD",
                "score": 0.7,
                "is_primary_key": False,
                "is_foreign_key": False,
                "metadata": {
                    "sample_values": [
                        {"value": "01"},
                        {"value": "02"},
                        "완료",
                    ]
                },
            }
        ],
        source="vector",
    )
    assert candidate.matched_columns[0].value_examples == ["01", "02", "완료"]


def test_candidate_blank_samples_are_empty() -> None:
    candidate = _candidate(
        {
            "id": 2,
            "db": "src",
            "source_name": "SRC",
            "schema_name": "S",
            "name": "EMPTY_TB",
            "original_name": "EMPTY_TB",
            "score": 0.1,
        },
        [
            {
                "name": "NOTE",
                "score": 0.2,
                "is_primary_key": False,
                "is_foreign_key": False,
                "metadata": {},
            }
        ],
        source="vector",
    )
    assert candidate.matched_columns[0].value_examples == []


def test_generate_prompt_uses_resolved_filters_as_where_sot() -> None:
    assert "query_plan.filters 중 resolution_status=resolved 인 항목은 WHERE SoT" in GENERATE_PROMPT
    assert "resolution_status=unresolved 인 필터의 value를 WHERE 리터럴로 쓰지 마라" in GENERATE_PROMPT
