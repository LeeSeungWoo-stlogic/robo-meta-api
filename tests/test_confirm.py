from app.schemas import (
    PlannedFilter,
    QueryAnalysis,
    QueryPlan,
    ResolvedEntity,
    ResolvedValue,
)
from app.services.t2sql.confirm import (
    align_entities_to_plan,
    reconcile_confirm,
    should_skip_confirm,
)
from app.services.t2sql.llm import CONFIRM_PROMPT


def _plan(*values: str) -> QueryPlan:
    return QueryPlan(
        completeness="complete",
        filters=[
            PlannedFilter(
                meaning="사업장 명칭",
                column="SRC.PLANT.CODE",
                operator="EQ",
                value=value,
                resolution_status="resolved",
                confidence=1.0,
            )
            for value in values
        ],
    )


def test_confirm_prompt_binds_to_resolved_filters() -> None:
    assert "resolution_status=resolved" in CONFIRM_PROMPT
    assert "기간을 missing으로 넣지 마라" in CONFIRM_PROMPT
    assert "query_analysis와 probe만 보고 재판하지 마라" in CONFIRM_PROMPT


def test_reconcile_drops_code_and_period_when_filter_resolved() -> None:
    result = reconcile_confirm(
        {"accept": False, "missing": ["사업장 코드", "기간"]},
        plan=_plan("354"),
        analysis=QueryAnalysis(status="complete", intent="sum"),
    )
    assert result == {"accept": True, "missing": []}


def test_reconcile_drops_master_role_slots() -> None:
    result = reconcile_confirm(
        {"accept": False, "missing": ["지역본부 마스터", "태그 마스터"]},
        plan=QueryPlan(completeness="partial"),
        analysis=QueryAnalysis(
            status="complete",
            intent="max",
            schema_roles=[],
        ),
    )
    assert result == {"accept": True, "missing": []}


def test_reconcile_drops_fact_slot_when_plan_left_unresolved() -> None:
    result = reconcile_confirm(
        {"accept": False, "missing": ["팩트 표"]},
        plan=QueryPlan(
            completeness="partial",
            unresolved_requirements=["팩트 표 미선정"],
        ),
        analysis=QueryAnalysis(status="complete", intent="max"),
    )
    assert result == {"accept": True, "missing": []}


def test_reconcile_drops_fact_slot_when_grain_unresolved() -> None:
    result = reconcile_confirm(
        {"accept": False, "missing": ["팩트 입도"]},
        plan=QueryPlan(
            completeness="partial",
            unresolved_requirements=["팩트 입도를 스토어 설명과 맞출 수 없음"],
        ),
        analysis=QueryAnalysis(status="complete", intent="sum"),
    )
    assert result == {"accept": True, "missing": []}


def test_reconcile_keeps_unrelated_status_slot() -> None:
    result = reconcile_confirm(
        {"accept": False, "missing": ["측정 상태가 현재 측정 중인 항목"]},
        plan=QueryPlan(completeness="partial"),
        analysis=QueryAnalysis(status="complete", intent="list"),
    )
    assert result["accept"] is False
    assert result["missing"] == ["측정 상태가 현재 측정 중인 항목"]


def test_reconcile_drops_year_slot_as_period() -> None:
    result = reconcile_confirm(
        {"accept": False, "missing": ["측정 연도"]},
        plan=QueryPlan(completeness="partial"),
        analysis=QueryAnalysis(status="complete", intent="sum"),
    )
    assert result == {"accept": True, "missing": []}


def test_reconcile_keeps_code_when_no_resolved_filter() -> None:
    result = reconcile_confirm(
        {"accept": False, "missing": ["facility"]},
        plan=QueryPlan(completeness="complete"),
        analysis=QueryAnalysis(status="complete", intent="x"),
    )
    assert result["accept"] is False
    assert result["missing"] == ["facility"]


def test_skip_confirm_when_plan_has_resolved_filters() -> None:
    assert should_skip_confirm(_plan("354")) is True
    assert should_skip_confirm(QueryPlan(completeness="complete")) is False
    assert should_skip_confirm(QueryPlan(completeness="partial")) is False


def test_reconcile_accepts_comma_string_missing() -> None:
    result = reconcile_confirm(
        {"accept": False, "missing": "사업장 코드,기간"},
        plan=_plan("354"),
        analysis=QueryAnalysis(status="complete", intent="sum"),
    )
    assert result == {"accept": True, "missing": []}


def test_align_entities_keeps_only_plan_codes() -> None:
    entity = ResolvedEntity(
        mention="아산정수장",
        entity_type="code",
        table="PLANT",
        name_column="NAME",
        code_column="CODE",
        values=[
            ResolvedValue(code="354", confidence=1.0),
            ResolvedValue(code="928", confidence=1.0),
        ],
    )
    aligned = align_entities_to_plan([entity], _plan("354"))
    assert [item.code for item in aligned[0].values] == ["354"]
