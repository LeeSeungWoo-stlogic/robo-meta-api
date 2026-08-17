from pathlib import Path

from app.schemas import PlannedJoinCondition, PlannedJoinPath, TableKey
from app.services.t2sql.join_on import missing_approved_on_predicates
from app.services.t2sql.llm import GENERATE_PROMPT

_REPO = Path(__file__).resolve().parents[1]


def _path(*pairs: tuple[str, str]) -> PlannedJoinPath:
    return PlannedJoinPath(
        from_table=TableKey(schema_name="S", table_name="LEFT_TB"),
        to_table=TableKey(schema_name="S", table_name="RIGHT_TB"),
        hop_count=1,
        conditions=[
            PlannedJoinCondition(from_=left, to=right)
            for left, right in pairs
        ],
    )


def test_accepts_sql_with_all_composite_on_columns() -> None:
    sql = (
        "SELECT 1 FROM src.S.LEFT_TB a "
        "JOIN src.S.RIGHT_TB b ON a.K1 = b.K1 AND a.K2 = b.K2"
    )
    missing = missing_approved_on_predicates(
        sql,
        [
            _path(
                ("S.LEFT_TB.K1", "S.RIGHT_TB.K1"),
                ("S.LEFT_TB.K2", "S.RIGHT_TB.K2"),
            )
        ],
    )
    assert missing == []


def test_rejects_dropped_composite_key_column() -> None:
    sql = "SELECT 1 FROM src.S.LEFT_TB a JOIN src.S.RIGHT_TB b ON a.K1 = b.K1"
    missing = missing_approved_on_predicates(
        sql,
        [
            _path(
                ("S.LEFT_TB.K1", "S.RIGHT_TB.K1"),
                ("S.LEFT_TB.K2", "S.RIGHT_TB.K2"),
            )
        ],
    )
    assert "S.LEFT_TB.K2=S.RIGHT_TB.K2" in missing


def test_skips_when_only_one_table_is_used() -> None:
    sql = "SELECT 1 FROM src.S.LEFT_TB"
    missing = missing_approved_on_predicates(
        sql,
        [_path(("S.LEFT_TB.K1", "S.RIGHT_TB.K1"))],
    )
    assert missing == []


def test_generate_prompt_uses_join_paths_as_on_sot() -> None:
    assert "query_plan.join_paths[].conditions는 ON SoT" in GENERATE_PROMPT
    assert "승인되지 않은 JOIN 엣지를 창작하지 마라" in GENERATE_PROMPT


def test_engine_guards_missing_approved_on() -> None:
    source = (_REPO / "app/services/t2sql/engine.py").read_text(encoding="utf-8")
    assert "missing_approved_on_predicates" in source
    assert "승인 JOIN ON이 빠짐" in source
