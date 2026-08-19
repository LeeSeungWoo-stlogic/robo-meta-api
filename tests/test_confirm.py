from app.schemas import (
    PlannedFilter,
    PlannedTable,
    QueryAnalysis,
    QueryPlan,
    ResolvedEntity,
    ResolvedValue,
)
from app.services.t2sql.confirm import (
    align_entities_to_plan,
    range_code_left_unresolved,
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
    assert "기간을 missing에 넣지 마라" in CONFIRM_PROMPT
    assert "IN 복수 코드는 충돌이 아니다" in CONFIRM_PROMPT
    assert "같은 유형 접미 동의어" in CONFIRM_PROMPT
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
    assert should_skip_confirm(QueryPlan(completeness="complete")) is True
    assert should_skip_confirm(QueryPlan(completeness="partial")) is True


def test_skip_confirm_requires_range_code_when_instance_present() -> None:
    period_only = QueryPlan(
        completeness="partial",
        filters=[
            PlannedFilter(
                meaning="측정 기간",
                column="SRC.FACT.MEAS_TM",
                operator="EQ",
                value="202510",
                resolution_status="resolved",
                confidence=1.0,
            )
        ],
    )
    analysis = QueryAnalysis(
        status="complete",
        procedure="list",
        meaning_status="complete",
        target="금강권역",
        primary_outputs=["정수장 목록"],
    )
    assert should_skip_confirm(period_only, analysis=analysis) is True
    assert should_skip_confirm(_plan("354"), analysis=analysis) is True


def test_reconcile_does_not_close_range_missing_by_table_role() -> None:
    result = reconcile_confirm(
        {"accept": False, "missing": ["권역"]},
        plan=QueryPlan(
            completeness="partial",
            required_tables=[
                PlannedTable(
                    schema_name="S",
                    table_name="HQ_TB",
                    role="지역본부",
                )
            ],
        ),
        analysis=QueryAnalysis(
            status="complete",
            intent="list",
            schema_roles=[],
        ),
    )
    assert result["accept"] is False
    assert result["missing"] == ["권역"]


def test_reconcile_keeps_code_when_only_period_resolved() -> None:
    period_only = QueryPlan(
        completeness="partial",
        filters=[
            PlannedFilter(
                meaning="측정 기간",
                column="SRC.FACT.MEAS_TM",
                operator="EQ",
                value="202510",
                resolution_status="resolved",
                confidence=1.0,
            )
        ],
    )
    result = reconcile_confirm(
        {"accept": False, "missing": ["facility"]},
        plan=period_only,
        analysis=QueryAnalysis(status="complete", intent="x"),
    )
    assert result["accept"] is False
    assert result["missing"] == ["facility"]


def test_range_code_left_unresolved() -> None:
    assert range_code_left_unresolved(
        QueryPlan(
            completeness="partial",
            unresolved_requirements=["범위 코드 미결합"],
        )
    )


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


def test_reconcile_keeps_same_column_in_codes() -> None:
    plan = QueryPlan(
        completeness="complete",
        filters=[
            PlannedFilter(
                meaning="코드매핑:금강유역본부",
                column="S.HQ_TB.BNB_CODE",
                operator="IN",
                value="701,902",
                resolution_status="resolved",
                confidence=1.0,
            )
        ],
    )
    result = reconcile_confirm(
        {
            "accept": False,
            "missing": ["코드 충돌"],
            "drop_codes": ["701", "902"],
        },
        plan=plan,
        analysis=QueryAnalysis(status="complete", intent="list"),
    )
    assert result["accept"] is True
    assert result["missing"] == []


def test_type_suffix_alias_is_not_mention_mismatch() -> None:
    plan = QueryPlan(
        completeness="complete",
        required_tables=[
            PlannedTable(schema_name="rwis", table_name="rdibonbu_tb", role="지역본부")
        ],
        filters=[
            PlannedFilter(
                meaning="코드매핑:금강유역본부(충청)",
                column="rwis.RDIBONBU_TB.BNB_CODE",
                operator="IN",
                value="701,902",
                resolution_status="resolved",
                confidence=1.0,
            )
        ],
    )
    entities = [
        ResolvedEntity(
            mention="금강유역본부(충청)",
            entity_type="code",
            table="rdibonbu_tb",
            name_column="BNB_NAME",
            values=[ResolvedValue(code="902", confidence=1.0)],
        ),
        ResolvedEntity(
            mention="금강유역본부",
            entity_type="code",
            table="rdibonbu_tb",
            name_column="BNB_NAME",
            values=[ResolvedValue(code="701", confidence=1.0)],
        ),
    ]
    result = reconcile_confirm(
        {"accept": True, "missing": []},
        plan=plan,
        analysis=QueryAnalysis(
            status="complete",
            procedure="list",
            target="금강권역",
            primary_outputs=["정수장"],
        ),
        entities=entities,
        query="금강권역 정수장 목록",
    )
    assert result["accept"] is True
    assert result["missing"] == []


def test_range_unresolved_ignored_when_codes_resolved() -> None:
    plan = QueryPlan(
        completeness="partial",
        unresolved_requirements=["범위 코드 미결합"],
        filters=[
            PlannedFilter(
                meaning="코드매핑:화성정수장",
                column="S.PLANT.SUJ_CODE",
                operator="EQ",
                value="617",
                resolution_status="resolved",
            )
        ],
    )
    assert range_code_left_unresolved(plan) is False


def test_reconcile_rejects_codes_not_in_plan() -> None:
    result = reconcile_confirm(
        {"accept": True, "missing": [], "add_codes": ["999"]},
        plan=_plan("354"),
        analysis=QueryAnalysis(status="complete", intent="sum"),
    )
    assert result["accept"] is False
    assert any("계획에 없는 코드" in item for item in result["missing"])


def test_align_entities_keeps_in_codes() -> None:
    entity = ResolvedEntity(
        mention="금강권역",
        entity_type="region",
        table="HQ_TB",
        name_column="NAME",
        code_column="BNB_CODE",
        values=[
            ResolvedValue(code="701", confidence=1.0),
            ResolvedValue(code="902", confidence=1.0),
        ],
    )
    plan = QueryPlan(
        completeness="complete",
        filters=[
            PlannedFilter(
                meaning="코드매핑:금강유역본부",
                column="S.HQ_TB.BNB_CODE",
                operator="IN",
                value="701,902",
                resolution_status="resolved",
            )
        ],
    )
    aligned = align_entities_to_plan([entity], plan)
    assert [item.code for item in aligned[0].values] == ["701", "902"]
