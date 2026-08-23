from pathlib import Path

from app.schemas import (
    PlannedFilter,
    PlannedJoinCondition,
    PlannedJoinPath,
    PlannedTable,
    QueryAnalysis,
    QueryPlan,
    TableKey,
)
from app.services.t2sql.join_on import guard_generated_sql, missing_approved_on_predicates
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


def test_rejects_on_when_table_pair_differs() -> None:
    sql = (
        "SELECT 1 FROM src.S.LEFT_TB a "
        "JOIN src.S.RIGHT_TB b ON 1=1 "
        "JOIN src.S.OTHER_TB c ON a.K1 = c.K1 AND a.K2 = c.K2"
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
    assert "S.LEFT_TB.K1=S.RIGHT_TB.K1" in missing


def test_engine_guards_missing_approved_on() -> None:
    source = (_REPO / "app/services/t2sql/engine.py").read_text(encoding="utf-8")
    assert "guard_generated_sql" in source
    assert "승인 JOIN ON이 빠짐" in (
        (_REPO / "app/services/t2sql/join_on.py").read_text(encoding="utf-8")
    )


def test_list_rejects_tag_hub_join() -> None:
    sql = (
        "SELECT DISTINCT t.SUJ_NAME FROM src.S.PLANT_TB t "
        "JOIN src.S.TAG_TB g ON t.BNB_CODE = g.BNB_CODE"
    )
    plan = QueryPlan(
        completeness="complete",
        required_tables=[
            PlannedTable(schema_name="S", table_name="PLANT_TB", role="사업장"),
            PlannedTable(schema_name="S", table_name="TAG_TB", role="태그 마스터"),
        ],
        answer_axis=["정수장"],
    )
    analysis = QueryAnalysis(
        status="complete",
        procedure="list",
        primary_outputs=["정수장"],
    )
    reason = guard_generated_sql(sql, plan, analysis)
    assert reason is not None
    assert "측정 허브 JOIN" in reason


def test_tag_list_allows_tag_table() -> None:
    sql = "SELECT g.TAG_NAME FROM src.S.TAG_TB g"
    plan = QueryPlan(
        completeness="complete",
        required_tables=[
            PlannedTable(schema_name="S", table_name="TAG_TB", role="태그 마스터"),
        ],
        answer_axis=["태그"],
    )
    analysis = QueryAnalysis(
        status="complete",
        procedure="list",
        primary_outputs=["태그"],
    )
    assert guard_generated_sql(sql, plan, analysis) is None


def test_type_only_list_does_not_require_code_filter() -> None:
    sql = "SELECT DISTINCT t.SUJ_NAME FROM src.S.PLANT_TB t"
    plan = QueryPlan(
        completeness="complete",
        required_tables=[
            PlannedTable(schema_name="S", table_name="PLANT_TB", role="사업장"),
        ],
        answer_axis=["정수장"],
        filters=[],
    )
    analysis = QueryAnalysis(
        status="complete",
        procedure="list",
        primary_outputs=["정수장"],
    )
    assert guard_generated_sql(sql, plan, analysis) is None


def test_resolved_code_must_appear_quoted() -> None:
    sql = "SELECT t.SUJ_NAME FROM src.S.PLANT_TB t"
    plan = QueryPlan(
        completeness="complete",
        required_tables=[
            PlannedTable(schema_name="S", table_name="PLANT_TB", role="사업장"),
        ],
        filters=[
            PlannedFilter(
                meaning="범위",
                column="S.PLANT_TB.BNB_CODE",
                operator="EQ",
                value="902",
                resolution_status="resolved",
            )
        ],
    )
    reason = guard_generated_sql(
        sql,
        plan,
        QueryAnalysis(status="complete", procedure="list"),
    )
    assert reason is not None
    assert "resolved 코드 필터" in reason
    ok = guard_generated_sql(
        "SELECT t.SUJ_NAME FROM src.S.PLANT_TB t WHERE t.BNB_CODE = '902'",
        plan,
        QueryAnalysis(status="complete", procedure="list"),
    )
    assert ok is None


def test_plant_list_accepts_group_by_without_distinct_keyword() -> None:
    sql = (
        "SELECT t.SUJ_CODE, t.SUJ_NAME FROM src.S.TAG_DIM t "
        "GROUP BY t.SUJ_CODE, t.SUJ_NAME"
    )
    plan = QueryPlan(
        completeness="complete",
        required_tables=[
            PlannedTable(
                schema_name="S",
                table_name="TAG_DIM",
                role="태그 마스터",
                required_columns=["SUJ_CODE", "SUJ_NAME"],
            ),
        ],
        answer_axis=["정수장"],
    )
    analysis = QueryAnalysis(
        status="complete",
        procedure="list",
        primary_outputs=["정수장"],
    )
    assert guard_generated_sql(sql, plan, analysis) is None


def test_plant_list_rejects_tag_grain_select() -> None:
    sql = "SELECT t.TAGSN, t.SUJ_NAME FROM src.S.TAG_DIM t"
    plan = QueryPlan(
        completeness="complete",
        required_tables=[
            PlannedTable(
                schema_name="S",
                table_name="TAG_DIM",
                role="태그 마스터",
                required_columns=["SUJ_CODE", "SUJ_NAME"],
            ),
        ],
        answer_axis=["정수장"],
    )
    analysis = QueryAnalysis(
        status="complete",
        procedure="list",
        primary_outputs=["정수장"],
    )
    reason = guard_generated_sql(sql, plan, analysis)
    assert reason is not None
    assert "DISTINCT 또는 GROUP BY" in reason
