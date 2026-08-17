from app.services.decision_postgres.helpers import _candidate
from app.services.t2sql.llm import GENERATE_PROMPT


def test_candidate_maps_serving_descriptions() -> None:
    candidate = _candidate(
        {
            "id": 1,
            "db": "src",
            "source_name": "SRC",
            "schema_name": "S",
            "name": "SITE_TB",
            "original_name": "SITE_TB",
            "score": 0.8,
            "description": "원본 표 설명",
            "analyzed_description": "사업장 식별 코드와 명칭을 관리",
            "subject_area": "code",
        },
        [
            {
                "name": "SITE_CD",
                "score": 0.7,
                "is_primary_key": True,
                "is_foreign_key": False,
                "description": "사업장 코드",
                "analyzed_description": "사업장을 구분하는 식별 코드",
            }
        ],
        source="vector",
    )
    assert candidate.table_comment == "원본 표 설명"
    assert candidate.description == "사업장 식별 코드와 명칭을 관리"
    assert candidate.matched_columns[0].column_name_kr == "사업장 코드"
    assert candidate.matched_columns[0].description == "사업장을 구분하는 식별 코드"


def test_candidate_blank_description_is_none() -> None:
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
        [],
        source="vector",
    )
    assert candidate.description is None
    assert candidate.table_comment is None


def test_generate_prompt_does_not_use_candidate_description_as_sot() -> None:
    assert "query_plan.required_tables의 table_name이 SQL 식별자" in GENERATE_PROMPT
    assert "용도·컨텍스트 힌트" not in GENERATE_PROMPT
