"""Universal Serving Plan builder for robo-meta-api v2 contract."""
from __future__ import annotations

from typing import Any, List, Optional
from ..schemas import (
    DecisionCandidate,
    ExecutionContext,
    QueryAnalysis,
    QueryPlan,
    ResolvedEntity,
    UniversalEntity,
    UniversalFilter,
    UniversalMetric,
    UniversalServingPlan,
)


def build_universal_plan(
    *,
    query: str,
    plan: Optional[QueryPlan],
    entities: List[ResolvedEntity],
    candidates: List[DecisionCandidate],
    analysis: Optional[QueryAnalysis],
    execution_context: Optional[ExecutionContext] = None,
) -> UniversalServingPlan:
    """Constructs a universal entity-metric-filter plan without table-name guessing or substring TAG matching."""
    univ_entities: List[UniversalEntity] = []
    univ_metrics: List[UniversalMetric] = []
    univ_filters: List[UniversalFilter] = []
    tags: List[str] = []
    tagsn: List[str] = []

    # 1. Map Resolved Entities (dam, observatory, facility, tag, station, pipe, etc.)
    for e in entities:
        etype = e.entity_type
        code_val = e.values[0].code if e.values else None
        label_val = e.values[0].label if (e.values and e.values[0].label) else e.mention
        conf = getattr(e.values[0], "confidence", 1.0) if e.values else 1.0
        
        univ_entities.append(
            UniversalEntity(
                entity_type=etype,
                entity_id=code_val,
                entity_name=label_val,
                table_name=e.table,
                join_key=e.code_column,
                confidence=conf,
            )
        )
        # Populate backward-compatible tags ONLY for actual tag entities
        if etype == "tag" and code_val:
            if code_val not in tags:
                tags.append(code_val)
            if label_val and label_val not in tagsn:
                tagsn.append(label_val)

    # 2. Extract Filters from QueryPlan
    if plan and plan.filters:
        for f in plan.filters:
            col = f.column or ""
            val = f.value
            op = f.operator or "="
            univ_filters.append(
                UniversalFilter(
                    column_name=col,
                    operator=op,
                    value=val,
                )
            )

    # 3. Extract Metrics from Plan Aggregation & Analysis
    if plan and plan.aggregation and (plan.aggregation.value_column or getattr(plan.aggregation, "measure_column", None)):
        mcol = plan.aggregation.value_column or getattr(plan.aggregation, "measure_column", None)
        mfunc = plan.aggregation.function or "AVG"
        univ_metrics.append(
            UniversalMetric(
                metric_name=mcol,
                column_name=mcol,
                aggregation=mfunc,
            )
        )
    elif analysis and analysis.measurement:
        m_name = analysis.measurement.name or analysis.measurement.expression or "value"
        m_func = analysis.measurement.aggregation or "AVG"
        univ_metrics.append(
            UniversalMetric(
                metric_name=m_name,
                column_name=m_name,
                aggregation=m_func,
            )
        )

    # 4. Determine system_type strictly from Store Source (never from table name guessing)
    source_name = None
    if execution_context and execution_context.source_name:
        source_name = execution_context.source_name
    elif candidates and candidates[0].source_name:
        source_name = candidates[0].source_name
    elif candidates and candidates[0].db:
        source_name = candidates[0].db

    system_type = (source_name or "UNKNOWN").upper()

    return UniversalServingPlan(
        system_type=system_type,
        entities=univ_entities,
        metrics=univ_metrics,
        filters=univ_filters,
        tags=tags,
        tagsn=tagsn,
    )
