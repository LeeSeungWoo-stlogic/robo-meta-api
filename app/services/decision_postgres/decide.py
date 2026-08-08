from __future__ import annotations

from collections import defaultdict
from typing import Any

from ...runtime_config import get_runtime
from ...schemas import (
    DecisionResponse,
    ExecutionContext,
    PlannedTable,
    QueryPlan,
)
from ..decision_planner import (
    build_composite_edges,
    merge_axis_candidates,
    prune_by_score_gap,
    select_minimal_tables,
)
from ..embedding_provider import get_embedding_provider
from ..execution_context_resolver import (
    ExecutionBindingError,
    resolve_execution_context,
)
from ..metadata_repository import PostgresMetadataRepository
from ..query_analysis import (
    analysis_embedding_text,
    get_query_analyzer,
    role_embedding_text,
)
from .filters import _resolve_filters
from .helpers import (
    _candidate,
    _merge_column_hits,
    _provisional_source_instance_id,
    _resolved_entities,
    _same_source,
)
from .plan_format import (
    _planned_paths,
    _strategy,
    _table_key,
    _top_target,
    assemble_join_groups,
)
from .roles import (
    _apply_subject_area_ranking,
    _enrich_analysis_roles,
    _is_dimension_facility_query,
    _role_candidate_has_evidence,
    _role_candidate_score,
)


async def decide(
    repository: PostgresMetadataRepository,
    *,
    query: str,
    include_matched_columns: bool,
    column_top_m: int | None,
    auto_resolve_entities: bool,
    table_limit: int | None = None,
) -> DecisionResponse:
    runtime = get_runtime()
    decision = runtime.decision
    effective_top_k = (
        max(1, min(50, int(table_limit)))
        if table_limit is not None
        else decision.table_top_k
    )

    analysis = await get_query_analyzer().analyze(query)
    analysis = _enrich_analysis_roles(query, analysis)
    axis_texts: dict[str, str] = {"question": query}
    if analysis.status == "complete":
        hyde_text = analysis_embedding_text(analysis)
        if hyde_text:
            axis_texts["hyde"] = hyde_text
        for role in analysis.schema_roles:
            axis_texts[f"role:{role.role}"] = role_embedding_text(
                analysis,
                role,
            )
        for index, requirement in enumerate(analysis.filter_requirements):
            axis_texts[f"filter:{index}"] = "\n".join(
                value
                for value in [
                    requirement.meaning,
                    requirement.value_text or "",
                    analysis.intent,
                ]
                if value
            )

    provider = get_embedding_provider()
    axis_names = list(axis_texts)
    vectors = await provider.embed_batch([axis_texts[name] for name in axis_names])
    embeddings = dict(zip(axis_names, vectors))

    axis_results: dict[str, list[dict[str, Any]]] = {
        "question": await repository.search_tables(
            embeddings["question"],
            limit=effective_top_k,
        )
    }
    if "hyde" in embeddings:
        axis_results["hyde"] = await repository.search_tables(
            embeddings["hyde"],
            limit=effective_top_k,
        )

    preliminary = merge_axis_candidates(
        axis_results,
        question_weight=decision.question_weight,
        hyde_weight=decision.hyde_weight,
        role_weight=decision.role_weight,
        limit=effective_top_k,
    )
    selected_source_instance = (
        str(preliminary[0].get("source_instance_id") or "")
        if preliminary
        else ""
    )

    mappings = await repository.find_value_mappings(query)
    if not selected_source_instance:
        selected_source_instance = _provisional_source_instance_id([], mappings)
    mappings = [
        mapping
        for mapping in mappings
        if selected_source_instance
        and _same_source(mapping, selected_source_instance)
    ]

    role_candidates: dict[str, list[dict[str, Any]]] = {}
    if selected_source_instance and analysis.status == "complete":
        for role in analysis.schema_roles:
            axis = f"role:{role.role}"
            vector = embeddings.get(axis)
            if vector is None:
                continue
            rows = await repository.search_tables(
                vector,
                limit=decision.role_top_k,
                source_instance_id=selected_source_instance,
            )
            rows = [
                {
                    **row,
                    "vector_score": float(row.get("score") or 0.0),
                    "score": _role_candidate_score(role, row),
                    "role_evidence": _role_candidate_has_evidence(role, row),
                }
                for row in rows
            ]
            rows = [
                row
                for row in rows
                if row["role_evidence"]
                or float(row["vector_score"]) >= decision.role_semantic_floor
            ]
            rows.sort(
                key=lambda row: (
                    -float(row.get("score") or 0.0),
                    str(row.get("original_name") or row.get("name") or ""),
                )
            )
            if rows:
                role_cutoff = (
                    float(rows[0].get("score") or 0.0)
                    * decision.role_min_score_ratio
                )
                rows = [
                    row
                    for row in rows
                    if float(row.get("score") or 0.0) >= role_cutoff
                ]
            role_candidates[role.role] = rows
            axis_results[axis] = rows

    merged = merge_axis_candidates(
        axis_results,
        question_weight=decision.question_weight,
        hyde_weight=decision.hyde_weight,
        role_weight=decision.role_weight,
        limit=max(effective_top_k, len(analysis.schema_roles) * decision.role_top_k),
    )
    merged = [
        table
        for table in merged
        if not selected_source_instance
        or str(table.get("source_instance_id") or "")
        == selected_source_instance
    ]

    mapped_table_ids = {
        int(mapping["table_id"])
        for mapping in mappings
        if mapping.get("table_id") is not None
    }
    existing_ids = {int(table["id"]) for table in merged}
    mapped_tables = await repository.fetch_tables_by_ids(
        mapped_table_ids - existing_ids
    )
    for table in mapped_tables:
        if (
            selected_source_instance
            and str(table.get("source_instance_id") or "")
            != selected_source_instance
        ):
            continue
        table["score"] = 1.0
        table["axis_scores"] = {"value_mapping": 1.0}
        table["role_scores"] = {}
        merged.append(table)
    # Exact value-mapping hubs already present from vector search must not
    # keep a weak vector score and then be gap-pruned away.
    for table in merged:
        if int(table["id"]) in mapped_table_ids:
            table["score"] = max(float(table.get("score") or 0.0), 1.0)
            axes = dict(table.get("axis_scores") or {})
            axes["value_mapping"] = 1.0
            table["axis_scores"] = axes

    dimension_facility = _is_dimension_facility_query(query, analysis)
    merged = _apply_subject_area_ranking(
        merged,
        dimension_facility=dimension_facility,
    )

    pruned = prune_by_score_gap(
        merged,
        max_k=effective_top_k,
        gap_ratio=decision.score_gap_ratio,
        min_step=decision.score_min_step,
        top_radius=decision.score_top_radius,
    )

    edge_rows = (
        await repository.fetch_join_edges(
            source_instance_id=selected_source_instance
        )
        if selected_source_instance
        else []
    )
    edges = build_composite_edges(edge_rows)
    required_roles = [
        role.role
        for role in analysis.schema_roles
        if role.necessity == "required"
    ]
    optional_roles = [
        role.role
        for role in analysis.schema_roles
        if role.necessity == "optional"
    ]
    selection = select_minimal_tables(
        required_roles=required_roles,
        optional_roles=optional_roles,
        role_candidates=role_candidates,
        edges=edges,
        max_hops=decision.fk_max_hops,
        table_limit=effective_top_k,
        distinct_role_pairs={
            frozenset({requirement.from_role, requirement.to_role})
            for requirement in analysis.join_requirements
            if requirement.required
        },
    )

    if analysis.status != "complete":
        selection.selected_table_ids = {
            int(table["id"]) for table in pruned
        }
        selection.bridge_table_ids = set()
        selection.paths = []
        selection.unresolved = [
            "의미 분해 실패로 역할별 최소 테이블 집합을 확정하지 못함"
        ]

    all_table_ids = {
        int(table["id"]) for table in pruned
    } | selection.selected_table_ids
    fetched = await repository.fetch_tables_by_ids(all_table_ids)
    tables_by_id = {
        int(table["id"]): table for table in [*merged, *fetched]
    }
    for table in pruned:
        tables_by_id[int(table["id"])].update(table)

    ordered_tables = [
        table
        for table in pruned
        if int(table["id"]) in all_table_ids
    ]
    ordered_ids = {int(table["id"]) for table in ordered_tables}
    for table_id in sorted(selection.selected_table_ids - ordered_ids):
        table = tables_by_id.get(table_id)
        if table is not None:
            table.setdefault("score", 0.0)
            ordered_tables.append(table)

    column_ids = [int(table["id"]) for table in ordered_tables]
    columns = (
        await repository.search_columns(
            embeddings["question"],
            table_ids=column_ids,
            per_table_limit=column_top_m or decision.column_top_m,
        )
        if include_matched_columns
        else {}
    )
    if include_matched_columns:
        metric = str(analysis.measurement.metric or "").strip()
        if metric:
            metric_columns = await repository.search_columns(
                await provider.embed(metric),
                table_ids=column_ids,
                per_table_limit=column_top_m or decision.column_top_m,
            )
            columns = _merge_column_hits(columns, metric_columns)
        for table in ordered_tables:
            table_id = int(table["id"])
            table_score = float(table.get("score") or 0.0)
            column_score = max(
                (
                    float(item.get("score") or 0.0)
                    for item in columns.get(table_id, [])
                ),
                default=table_score,
            )
            if table_id not in selection.selected_table_ids:
                table["score"] = 0.5 * table_score + 0.5 * column_score
        ordered_tables.sort(
            key=lambda item: (
                0 if int(item["id"]) in selection.selected_table_ids else 1,
                -float(item.get("score") or 0.0),
            )
        )

    plan_table_ids = sorted(selection.selected_table_ids)
    planned_filters, filter_unresolved = await _resolve_filters(
        repository,
        requirements=analysis.filter_requirements,
        embeddings=embeddings,
        table_ids=plan_table_ids,
        tables_by_id=tables_by_id,
        mappings=mappings,
        minimum_similarity=decision.minimum_similarity,
    )
    unresolved = [*selection.unresolved, *filter_unresolved]
    if analysis.status == "degraded":
        completeness = "degraded"
    elif not selection.role_tables and required_roles:
        completeness = "failed"
    elif unresolved:
        completeness = "partial"
    else:
        completeness = "complete"

    planned_paths = _planned_paths(selection.paths, tables_by_id)
    role_by_name = {role.role: role for role in analysis.schema_roles}
    required_columns_by_id: dict[int, set[str]] = defaultdict(set)
    table_id_by_name = {
        str(table.get("original_name") or table.get("name") or "").lower(): table_id
        for table_id, table in tables_by_id.items()
    }
    for path in planned_paths:
        for condition in path.conditions:
            for fqn in (condition.from_, condition.to):
                parts = fqn.split(".")
                if len(parts) >= 3:
                    table_id = table_id_by_name.get(parts[-2].lower())
                    if table_id is not None:
                        required_columns_by_id[table_id].add(parts[-1])
    for planned_filter in planned_filters:
        parts = (planned_filter.column or "").split(".")
        if len(parts) >= 3:
            table_id = table_id_by_name.get(parts[-2].lower())
            if table_id is not None:
                required_columns_by_id[table_id].add(parts[-1])

    planned_by_id: dict[int, PlannedTable] = {}
    for role, table in selection.role_tables.items():
        table_id = int(table["id"])
        current = planned_by_id.get(table_id)
        if current is None:
            current = PlannedTable(
                **_table_key(table).model_dump(),
                table_id=table_id,
                role=role,
                roles=[role],
                necessity=role_by_name[role].necessity,
                required_columns=sorted(required_columns_by_id[table_id]),
                score=float(table.get("score") or 0.0),
            )
            planned_by_id[table_id] = current
        elif role not in current.roles:
            current.roles.append(role)
            if role_by_name[role].necessity == "required":
                current.necessity = "required"
    planned_tables = list(planned_by_id.values())
    bridge_tables = [
        _table_key(tables_by_id[table_id])
        for table_id in sorted(selection.bridge_table_ids)
        if table_id in tables_by_id
    ]
    plan = QueryPlan(
        completeness=completeness,
        strategy=_strategy(planned_paths, planned_filters),
        required_tables=planned_tables,
        bridge_tables=bridge_tables,
        join_paths=planned_paths,
        filters=planned_filters,
        unresolved_requirements=unresolved,
    )

    join_groups = assemble_join_groups(
        planned_paths=planned_paths,
        tables_by_id=tables_by_id,
        selected_table_ids=selection.selected_table_ids,
        bridge_table_ids=selection.bridge_table_ids,
        bridge_tables=bridge_tables,
        strategy=plan.strategy,
        required_roles=required_roles,
    )

    candidates = [
        _candidate(
            table,
            columns.get(int(table["id"]), []),
            source=(
                "name_rule"
                if int(table["id"]) in mapped_table_ids
                else "vector"
            ),
        )
        for table in ordered_tables[:effective_top_k]
    ]
    entities = _resolved_entities(mappings) if auto_resolve_entities else []
    allowed_objects = [
        str(
            tables_by_id[table_id].get("original_name")
            or tables_by_id[table_id].get("name")
            or ""
        )
        for table_id in plan_table_ids
        if table_id in tables_by_id
    ]
    execution_context = None
    if completeness == "complete" and selected_source_instance and allowed_objects:
        try:
            resolved = await resolve_execution_context(
                repository,
                source_instance_id=selected_source_instance,
                requested_objects=allowed_objects,
            )
            if resolved.source_name:
                execution_context = ExecutionContext(**resolved.public_dict())
            else:
                execution_context = None
        except ExecutionBindingError:
            execution_context = None

    secondary = sorted(
        {
            candidate.target_class
            for candidate in candidates
            if candidate.target_class
            not in {"unknown", _top_target(candidates)}
        }
    )
    return DecisionResponse(
        target=_top_target(candidates),
        secondary_targets=secondary,
        confidence=float(candidates[0].score) if candidates else 0.0,
        candidates=candidates,
        join_groups=join_groups,
        threshold_used={
            "minimum_similarity": decision.minimum_similarity,
            "table_top_k": effective_top_k,
            "table_limit": table_limit,
            "column_top_m": column_top_m or decision.column_top_m,
            "retrieval_axes": axis_names,
            "fk_max_hops": decision.fk_max_hops,
        },
        resolved_entities=entities,
        suggested_probes=[],
        resolution_status="complete" if entities else "partial",
        execution_context=execution_context,
        query_analysis=analysis,
        query_plan=plan,
    )
