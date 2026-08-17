from __future__ import annotations

from collections import defaultdict
from typing import Any

from ...runtime_config import get_runtime
from ...schemas import (
    DecisionResponse,
    ExecutionContext,
    GlossaryRoute,
    PlannedFilter,
    PlannedTable,
    QueryPlan,
)
from ..decision_planner import (
    build_composite_edges,
)
from ..execution_context_resolver import (
    ExecutionBindingError,
    resolve_execution_context,
)
from ..metadata_repository import PostgresMetadataRepository
from ..metadata_repository._search import SearchMixin
from ..query_analysis import get_query_analyzer
from .filters import _propagate_filters_along_fk
from .helpers import (
    _candidate,
    _provisional_source_instance_id,
    _resolve_subject_area,
    _resolved_entities,
    _same_source,
    _serving_logical_name,
)
from .table_type import list_table_type
from .plan_format import (
    _planned_paths,
    _strategy,
    _table_key,
    _top_target,
    assemble_join_groups,
)
from .aliases import expand_region_hq_aliases
from .grain import resolve_time_grain, year_window_narrows_to_month
from .period import parse_korean_period, week_mention
from .store_first import (
    chosen_labels,
    catalog_group_dimensions,
    group_dimension_needles,
    assemble_anchor_join_paths,
    drop_unjoinable_catalog_ids,
    drop_unselected_fact_tables,
    facts_joinable_to_mappings,
    narrow_facts_by_query_clock,
    narrow_facts_for_week,
    prefer_unique_fact_type,
    empty_meta_response,
    fact_unresolved_response,
    dimension_identity_column_names,
    fact_time_column_names,
    is_tag_master_table,
    measure_point_label_filters,
    promote_series_identity_tables,
    filter_mappings_to_labels,
    glossary_extras,
    is_month_grain_table,
    value_mappings_for_plan,
    is_dimension_list_query,
    location_group_tables,
    mapping_filters,
    mapping_labels,
    partition_mention_mappings,
    measure_column_names,
    period_filter_for_fact,
    pick_fact_tables,
    query_requests_fact,
    resolve_time_role,
    seed_table_ids,
    table_blob,
    unbound_period_filter,
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

    tokens = SearchMixin._question_mention_tokens(query)
    glossary_rows = await repository.find_glossary_routes(query)
    extras = [
        *glossary_extras(glossary_rows),
        *expand_region_hq_aliases(query, tokens),
    ]
    mappings = await repository.find_value_mappings(query, extra_mentions=extras)
    prefixes = SearchMixin.prefixes_for_unmatched(tokens, mapping_labels(mappings))
    if prefixes:
        extras = [*extras, *prefixes]
        mappings = await repository.find_value_mappings(
            query,
            extra_mentions=extras,
        )
    catalog = await repository.find_catalog_by_mentions(
        [*tokens, *extras],
    )
    catalog_mentions = list(catalog)
    selected_source = _provisional_source_instance_id(catalog, mappings)
    if selected_source:
        mappings = [
            mapping
            for mapping in mappings
            if _same_source(mapping, selected_source)
        ]
        catalog = [
            table
            for table in catalog
            if _same_source(table, selected_source)
        ]
    unique_mappings, ambiguous_mappings = partition_mention_mappings(mappings)
    seed_ids = seed_table_ids(unique_mappings, catalog)
    group_needles = group_dimension_needles(query, tokens, extras)
    group_dims = catalog_group_dimensions(
        catalog_mentions,
        group_needles,
    )
    group_ids = {
        int(table["id"])
        for table in group_dims
        if table.get("id") is not None
    }
    if not seed_ids and not group_ids:
        return empty_meta_response()

    store_hits = {
        "glossary": [
            {
                "mention": row.get("mention") or row.get("surface"),
                "standard_term": row.get("standard_term"),
                "word_korean": row.get("word_korean"),
                "definition": row.get("definition"),
                "abbreviation": row.get("abbreviation"),
                "english_name": row.get("english_name"),
                "aliases": row.get("aliases"),
                "surface": row.get("surface"),
            }
            for row in glossary_rows
        ],
        "value_mappings": [
            {
                "natural_value": row.get("natural_value"),
                "code_value": row.get("code_value"),
                "column_fqn": row.get("column_fqn"),
                "logical_name": row.get("logical_name"),
            }
            for row in unique_mappings
        ],
        "prefix_candidates": [
            {
                "natural_value": row.get("natural_value"),
                "code_value": row.get("code_value"),
                "column_fqn": row.get("column_fqn"),
            }
            for row in ambiguous_mappings
        ],
        "catalog": [
            {
                "logical_name": _serving_logical_name(table),
                "description": table.get("description"),
                "subject_area": table.get("subject_area"),
            }
            for table in catalog
        ],
    }
    analysis = await get_query_analyzer().analyze(query, store_hits)
    labels = chosen_labels(analysis)
    mappings = unique_mappings
    if labels:
        covered = {
            SearchMixin._compact_natural_text(str(row.get("matched_mention") or ""))
            for row in unique_mappings
        }
        covered |= {
            SearchMixin._compact_natural_text(str(row.get("natural_value") or ""))
            for row in unique_mappings
        }
        covered.discard("")
        missing_labels = [
            label
            for label in labels
            if SearchMixin._compact_natural_text(label)
            and not any(
                SearchMixin._compact_natural_text(label) in item
                or item in SearchMixin._compact_natural_text(label)
                for item in covered
            )
        ]
        if missing_labels:
            remapped = await repository.find_value_mappings(
                query,
                source_instance_id=selected_source or None,
                extra_mentions=[*extras, *missing_labels],
            )
            remapped = filter_mappings_to_labels(remapped, missing_labels)
            remapped, _ambiguous = partition_mention_mappings(remapped)
            if remapped:
                remapped_mentions = {
                    SearchMixin._compact_natural_text(
                        str(row.get("matched_mention") or "")
                    )
                    for row in remapped
                }
                remapped_mentions.discard("")
                kept = [
                    row
                    for row in mappings
                    if SearchMixin._compact_natural_text(
                        str(row.get("matched_mention") or "")
                    )
                    not in remapped_mentions
                ]
                mappings = kept + remapped
                if selected_source:
                    mappings = [
                        mapping
                        for mapping in mappings
                        if _same_source(mapping, selected_source)
                    ]
                seed_ids = seed_table_ids(mappings, catalog)

    if not seed_ids:
        seed_ids = set(group_ids)
    hops = max(1, int(decision.fk_max_hops))
    expanded = set(seed_ids) | group_ids
    frontier = set(seed_ids) | group_ids
    for _ in range(hops):
        neighbors = await repository.fk_neighbor_table_ids(frontier)
        nxt = neighbors - expanded
        if not nxt:
            break
        expanded |= nxt
        frontier = nxt

    fetched = await repository.fetch_tables_by_ids(expanded)
    if selected_source:
        fetched = [
            table
            for table in fetched
            if _same_source(table, selected_source)
        ]
    tables_by_id = {int(table["id"]): table for table in fetched}
    group_dims = catalog_group_dimensions(
        [*catalog, *fetched],
        group_needles,
    )
    group_ids = {
        int(table["id"])
        for table in group_dims
        if table.get("id") is not None
    }
    missing_group_ids = group_ids - set(tables_by_id)
    if missing_group_ids:
        extra_fetched = await repository.fetch_tables_by_ids(missing_group_ids)
        if selected_source:
            extra_fetched = [
                table
                for table in extra_fetched
                if _same_source(table, selected_source)
                or not str(table.get("source_instance_id") or "").strip()
            ]
        for table in extra_fetched:
            tables_by_id[int(table["id"])] = table
    parsed_period = parse_korean_period(query)
    week_parsed = parsed_period is not None and parsed_period.week_start is not None
    query_grain = resolve_time_grain(query)
    hint = query_grain or str(analysis.measurement.storage_type_hint or "").strip() or None
    if not hint:
        hint = resolve_time_grain(query, analysis)
    fact_pool = list(tables_by_id.values())
    apply_query_grain = True
    if week_parsed:
        fact_pool = [table for table in fact_pool if not is_month_grain_table(table)]
        hint = None
        apply_query_grain = False
    elif (
        parsed_period is not None
        and parsed_period.grain == "year"
        and year_window_narrows_to_month(query_grain)
    ):
        month_pool = [table for table in fact_pool if is_month_grain_table(table)]
        if month_pool:
            fact_pool = month_pool
            hint = "month"
    if query_requests_fact(query, parsed_period, analysis, mappings):
        facts, fact_unresolved = pick_fact_tables(
            fact_pool,
            hint,
            query=query,
            apply_query_grain=apply_query_grain,
        )
    else:
        facts, fact_unresolved = [], None
    edge_rows = (
        await repository.fetch_join_edges(source_instance_id=selected_source)
        if selected_source
        else []
    )
    edges = build_composite_edges(edge_rows)
    mapped_ids = seed_table_ids(mappings, [])
    if len(facts) > 1:
        facts = facts_joinable_to_mappings(
            facts,
            mapped_ids=mapped_ids,
            edges=edges,
            max_hops=hops,
        )
        if week_parsed:
            facts = narrow_facts_for_week(facts, query)
        if len(facts) > 1:
            facts = narrow_facts_by_query_clock(facts, query)
        if len(facts) > 1:
            facts = prefer_unique_fact_type(facts)
        if len(facts) > 1:
            fact_unresolved = fact_unresolved or "팩트 표 미선정"
            facts = []
        elif len(facts) == 1:
            fact_unresolved = None
        else:
            fact_unresolved = fact_unresolved or "팩트 표 미선정"
            facts = []
    if mapped_ids or facts or group_ids:
        selected_ids = set(mapped_ids) | group_ids
        for fact in facts:
            selected_ids.add(int(fact["id"]))
    else:
        selected_ids = set(seed_ids)
    selected_ids &= set(tables_by_id)
    if is_dimension_list_query(query) and not facts:
        for table in tables_by_id.values():
            if list_table_type(_resolve_subject_area(table)) != "Code":
                continue
            blob = SearchMixin._compact_natural_text(table_blob(table))
            if any(word in blob for word in ("별량", "측정항목", "태그")):
                table_id = table.get("id")
                if table_id is not None:
                    selected_ids.add(int(table_id))
    for table in location_group_tables(list(tables_by_id.values()), query):
        table_id = table.get("id")
        if table_id is not None:
            selected_ids.add(int(table_id))
    if not selected_ids:
        if fact_unresolved:
            return fact_unresolved_response(analysis, fact_unresolved)
        return empty_meta_response(analysis)
    catalog_ids = {
        int(table["id"])
        for table in [*catalog, *catalog_mentions]
        if table.get("id") is not None
    }
    selected_ids = drop_unjoinable_catalog_ids(
        selected_ids,
        mapped_ids=mapped_ids,
        fact_ids={int(fact["id"]) for fact in facts},
        catalog_ids=catalog_ids,
        edges=edges,
        max_hops=hops,
        tables_by_id=tables_by_id,
    )
    selected_ids = drop_unselected_fact_tables(
        selected_ids,
        chosen_fact_ids={int(fact["id"]) for fact in facts},
        tables_by_id=tables_by_id,
        mapped_ids=mapped_ids,
    )
    if not selected_ids:
        if fact_unresolved:
            return fact_unresolved_response(analysis, fact_unresolved)
        return empty_meta_response(analysis)
    unresolved: list[str] = []
    if fact_unresolved:
        unresolved.append(fact_unresolved)
    paths, origin, join_unresolved = assemble_anchor_join_paths(
        selected_ids,
        edges=edges,
        max_hops=hops,
        fact_ids={int(fact["id"]) for fact in facts},
        mapped_ids=mapped_ids,
        tables_by_id=tables_by_id,
    )
    unresolved.extend(join_unresolved)
    bridge_ids = origin - selected_ids
    selected_ids |= origin
    bridge_ids = promote_series_identity_tables(
        bridge_ids,
        tables_by_id=tables_by_id,
        query=query,
    )

    columns = await repository.fetch_approved_columns(sorted(selected_ids))
    planned_filters = mapping_filters(mappings, query=query)
    planned_filters.extend(
        measure_point_label_filters(
            query,
            mappings,
            tables_by_id=tables_by_id,
            columns_by_id=columns,
            table_ids=selected_ids,
        )
    )
    if facts:
        for fact in facts:
            period = period_filter_for_fact(
                query,
                fact,
                columns.get(int(fact["id"]), []),
            )
            if period is not None:
                planned_filters.append(period)
                if period.resolution_status == "unresolved":
                    unresolved.append("기간 컬럼 메타 없음")
    elif parsed_period is not None:
        planned_filters.append(unbound_period_filter(parsed_period))
    elif week_mention(query):
        planned_filters.append(
            PlannedFilter(
                meaning="측정 기간",
                column=None,
                operator="BETWEEN",
                resolution_status="unresolved",
            )
        )
    planned_filters = _propagate_filters_along_fk(
        planned_filters,
        edge_rows,
        anchor_table_ids=selected_ids,
        tables_by_id=tables_by_id,
    )

    planned_paths = _planned_paths(paths, tables_by_id)
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
    for fact in facts:
        fact_id = int(fact["id"])
        required_columns_by_id[fact_id].update(
            measure_column_names(columns.get(fact_id, []))
        )
        required_columns_by_id[fact_id].update(
            fact_time_column_names(columns.get(fact_id, []))
        )
    for table_id, table in tables_by_id.items():
        if int(table_id) not in selected_ids:
            continue
        typed = list_table_type(_resolve_subject_area(table))
        if typed in {"Fact", "Raw"}:
            continue
        if typed not in {"Dimension", "Code"} and not is_tag_master_table(table):
            continue
        required_columns_by_id[int(table_id)].update(
            dimension_identity_column_names(
                columns.get(int(table_id), []),
                table,
            )
        )

    planned_tables = []
    for table_id in sorted(selected_ids - bridge_ids):
        table = tables_by_id.get(table_id)
        if table is None:
            continue
        role = str(_serving_logical_name(table) or table.get("original_name") or "")
        planned_tables.append(
            PlannedTable(
                **_table_key(table).model_dump(),
                table_id=table_id,
                role=role,
                roles=[role],
                necessity="required",
                required_columns=sorted(required_columns_by_id[table_id]),
                score=1.0,
            )
        )
    bridge_tables = [
        _table_key(tables_by_id[table_id])
        for table_id in sorted(bridge_ids)
        if table_id in tables_by_id
    ]
    if not planned_tables and not planned_filters:
        completeness = "failed"
    elif unresolved or analysis.status == "degraded":
        completeness = "partial"
    else:
        completeness = "complete"
    plan = QueryPlan(
        completeness=completeness,
        strategy=_strategy(planned_paths, planned_filters),
        required_tables=planned_tables,
        bridge_tables=bridge_tables,
        join_paths=planned_paths,
        filters=planned_filters,
        unresolved_requirements=unresolved,
        time_role=resolve_time_role(query, parsed_period),
    )
    join_groups = assemble_join_groups(
        planned_paths=planned_paths,
        tables_by_id=tables_by_id,
        selected_table_ids=selected_ids - bridge_ids,
        bridge_table_ids=bridge_ids,
        bridge_tables=bridge_tables,
        strategy=plan.strategy,
        required_roles=[table.role for table in planned_tables],
    )
    ordered_tables = [
        tables_by_id[table_id]
        for table_id in sorted(selected_ids)
        if table_id in tables_by_id
    ]
    candidates = [
        _candidate(
            table,
            columns.get(int(table["id"]), []) if include_matched_columns else [],
            source="name_rule" if int(table["id"]) in mapped_ids else "schema_rule",
        )
        for table in ordered_tables[:effective_top_k]
    ]
    entities = (
        _resolved_entities(value_mappings_for_plan(mappings, query))
        if auto_resolve_entities
        else []
    )
    allowed_objects = [
        str(tables_by_id[table_id].get("original_name") or tables_by_id[table_id].get("name") or "")
        for table_id in sorted(selected_ids)
        if table_id in tables_by_id
    ]
    execution_context = None
    if selected_source and allowed_objects:
        try:
            resolved = await resolve_execution_context(
                repository,
                source_instance_id=selected_source,
                requested_objects=allowed_objects,
            )
            if resolved.source_name:
                execution_context = ExecutionContext(**resolved.public_dict())
        except ExecutionBindingError:
            execution_context = None
    glossary_routes = [
        GlossaryRoute(
            mention=str(row.get("mention") or ""),
            standard_term=str(row.get("standard_term") or ""),
            definition=str(row.get("definition") or "") or None,
        )
        for row in glossary_rows
        if row.get("mention") and row.get("standard_term")
    ]
    return DecisionResponse(
        target=_top_target(candidates),
        secondary_targets=[],
        confidence=1.0 if candidates else 0.0,
        candidates=candidates,
        join_groups=join_groups,
        threshold_used={
            "table_top_k": effective_top_k,
            "fk_max_hops": hops,
            "retrieval_axes": ["store"],
        },
        resolved_entities=entities,
        suggested_probes=[],
        resolution_status="complete" if entities or planned_tables else "failed",
        execution_context=execution_context,
        query_analysis=analysis,
        query_plan=plan,
        glossary_routes=glossary_routes,
    )
