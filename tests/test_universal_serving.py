import pytest
from app.schemas import (
    DecisionCandidate,
    ExecutionContext,
    PlannedFilter,
    PlanAggregation,
    QueryPlan,
    ResolvedEntity,
    ResolvedValue,
)
from app.services.universal_serving import build_universal_plan


def test_build_universal_plan_rwis_compatibility():
    # RWIS style input: tag entity and tagsn filter
    entity = ResolvedEntity(
        mention="탁도",
        entity_type="tag",
        table="RDITAG_TB",
        name_column="TAG_NAME",
        code_column="TAGSN",
        values=[ResolvedValue(code="100120", label="화성정수장_탁도")],
    )
    plan = QueryPlan(
        completeness="complete",
        filters=[
            PlannedFilter(
                column="RDITAG_TB.TAGSN",
                operator="EQ",
                value="100120",
                meaning="filter",
                necessity="required",
            )
        ],
        aggregation=PlanAggregation(
            value_column="VAL",
            function="AVG",
        ),
    )
    candidate = DecisionCandidate(
        source_name="rwis_mart_view",
        schema_name="RWIS",
        table_name="RDF01HH_TB",
    )

    univ_plan = build_universal_plan(
        query="화성정수장 평균 탁도",
        plan=plan,
        entities=[entity],
        candidates=[candidate],
        analysis=None,
    )

    assert univ_plan.system_type == "RWIS_MART_VIEW"
    assert len(univ_plan.entities) == 1
    assert univ_plan.entities[0].entity_id == "100120"
    assert len(univ_plan.metrics) == 1
    assert univ_plan.metrics[0].column_name == "VAL"
    # Verify backward compatibility tags projection from genuine tag entity
    assert "100120" in univ_plan.tags
    assert "화성정수장_탁도" in univ_plan.tagsn


def test_build_universal_plan_hdaps_multi_metric():
    # HDAPS style input: Dam entity and wide metrics
    entity = ResolvedEntity(
        mention="소양강댐",
        entity_type="dam",
        table="RS_OBSMST",
        name_column="DAMNM",
        code_column="DAMCD",
        values=[ResolvedValue(code="1012110", label="소양강댐")],
    )
    plan = QueryPlan(
        completeness="complete",
        filters=[
            PlannedFilter(
                column="DUBHRDAMIF.DAMCD",
                operator="EQ",
                value="1012110",
                meaning="filter",
                necessity="required",
            )
        ],
        aggregation=PlanAggregation(
            value_column="IQTY",
            function="AVG",
        ),
    )
    candidate = DecisionCandidate(
        source_name="hdaps_source",
        schema_name="NBEAVER",
        table_name="DUBHRDAMIF",
    )

    univ_plan = build_universal_plan(
        query="소양강댐 최근 시간별 유입량",
        plan=plan,
        entities=[entity],
        candidates=[candidate],
        analysis=None,
    )

    assert univ_plan.system_type == "HDAPS_SOURCE"
    assert len(univ_plan.entities) == 1
    assert univ_plan.entities[0].entity_type == "dam"
    assert univ_plan.entities[0].entity_id == "1012110"
    assert univ_plan.metrics[0].column_name == "IQTY"
