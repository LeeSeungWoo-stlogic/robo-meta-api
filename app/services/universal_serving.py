"""Universal Serving Plan builder for robo-meta-api v2 contract."""
from __future__ import annotations

from typing import Any, List, Optional
from ..schemas import (
    DecisionCandidate,
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
) -> UniversalServingPlan:
    """Constructs a universal entity-metric-filter plan with backward-compatible tags projection."""
    univ_entities: List[UniversalEntity] = []
    univ_metrics: List[UniversalMetric] = []
    univ_filters: List[UniversalFilter] = []
    tags: List[str] = []
    tagsn: List[str] = []

    # 1. Map Resolved Entities
    for e in entities:
        etype = e.entity_type
        # Extract code value and label
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
        if etype == "tag" and code_val:
            tags.append(code_val)
            if label_val:
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
            # Check if this filter is for tagsn / tag
            if "TAGSN" in col.upper() or "TAG" in col.upper():
                if isinstance(val, list):
                    for v in val:
                        if str(v) not in tags:
                            tags.append(str(v))
                elif val and str(val) not in tags:
                    tags.append(str(val))

    # 3. Extract Metrics from Plan Aggregation & Candidates
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

    # Detect system domain type
    t_names = [c.table_name.upper() for c in candidates]
    if any("DAM" in t or "RF" in t or "WL" in t for t in t_names):
        sys_type = "HDAPS"
    elif any("TAG" in t or "RDF" in t or "RDR" in t or "RWIS" in t for t in t_names):
        sys_type = "RWIS"
    elif any("GIS" in t or "PIPE" in t or "VALVE" in t for t in t_names):
        sys_type = "GIOS"
    else:
        sys_type = "AUTO"

    return UniversalServingPlan(
        system_type=sys_type,
        entities=univ_entities,
        metrics=univ_metrics,
        filters=univ_filters,
        tags=tags,
        tagsn=tagsn,
    )
