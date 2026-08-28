from __future__ import annotations

import logging
import time
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
from ..query_analysis import degraded_analysis, get_query_analyzer
from .aliases import (
    TypeGroup,
    _active_groups,
    annotate_matched_mention,
    expand_region_hq_aliases,
    peel_type_suffix,
    range_surface_has_instance,
    type_product_surfaces,
    unique_code_rows,
)
from ..meaning_slots import (
    answer_axis_from_analysis,
    axis_mention,
    catalog_needles_from_analysis,
    compact,
    filter_needles_from_analysis,
    glossary_synonyms_for_needles,
    meaning_failed,
    metric_needles_from_analysis,
    range_slots_from_analysis,
    unique_needles,
    time_role_from_procedure,
    synonym_members_for_needles,
)
from .filters import _propagate_filters_along_fk
from .helpers import (
    _candidate,
    _column_tail,
    _provisional_source_instance_id,
    _resolve_subject_area,
    _resolved_entities,
    _same_source,
    _serving_logical_name,
    glossary_surfaces,
    select_question_columns,
)
from .table_type import list_table_type
from .plan_format import (
    _planned_paths,
    _strategy,
    _table_key,
    _top_target,
    assemble_join_groups,
)
from .grain import TimeGrain, resolve_time_grain, year_window_narrows_to_month
from .period import parse_period_from_query, week_mention
from .select_store import (
    apply_store_selection,
    compact_store_recall,
    select_from_store,
)
from .suffix_store import load_term_synonym_groups, load_type_groups
from .candidate_evidence import build_candidate_evidence
from .data_process import (
    bound_tagsn,
    load_process_rows,
    probe_metric_tag_rows,
    replace_tagsn_filter,
    source_uses_process_rules,
    tag_probe_needles,
    tagsn_codes_from_rows,
    usage_keep_rows,
)
from .store_first import (
    chosen_labels,
    catalog_group_dimensions,
    group_dimension_needles,
    assemble_anchor_join_paths,
    drop_unjoinable_catalog_ids,
    drop_unselected_fact_tables,
    facts_joinable_to_mappings,
    list_stays_on_selected_tables,
    narrow_facts_by_query_clock,
    narrow_facts_for_week,
    prefer_unique_fact_type,
    empty_meta_response,
    embedding_recall_mappings,
    fact_unresolved_response,
    dimension_identity_column_names,
    fact_time_column_names,
    aggregation_contract,
    approved_code_mappings,
    is_catalog_list_query,
    is_tag_master_table,
    list_axis_identity_column_names,
    list_axis_skips_tag_identity,
    measure_point_label_filters,
    promote_series_identity_tables,
    filter_mappings_to_labels,
    is_month_grain_table,
    value_mappings_for_plan,
    location_group_tables,
    mapping_filters,
    mapping_labels,
    _plan_column_fqn,
    partition_mention_mappings,
    period_required_response,
    project_code_mappings_to_hub,
    measure_column_names,
    period_filter_for_fact,
    pick_fact_tables,
    prefer_day_grain_facts,
    query_requests_fact,
    rewrite_mapping_filters_onto_facts,
    measurement_needs_period,
    RANGE_CODE_UNRESOLVED,
    range_unresolved_response,
    seed_table_ids,
    unbound_period_filter,
)

logger = logging.getLogger(__name__)


def _needle_covered_by_bound(needle: str, bound: list[dict[str, Any]]) -> bool:
    key = SearchMixin._compact_natural_text(needle)
    if not key:
        return False
    for row in bound:
        for field in ("matched_mention", "natural_value"):
            mention = SearchMixin._compact_natural_text(str(row.get(field) or ""))
            if mention and (mention in key or key in mention):
                return True
    return False


def _rows_matching_needle(rows: list[dict[str, Any]], needle: str) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if SearchMixin._label_matches_needle(str(row.get("natural_value") or ""), needle)
    ]


def _tagsn_column_fqn(
    facts: list[dict[str, Any]],
    columns_by_id: dict[int, list[dict[str, Any]]],
    tables_by_id: dict[int, dict[str, Any]],
    selected_ids: set[int],
    planned_filters: list[PlannedFilter] | None = None,
) -> str | None:
    for fact in facts:
        cols = columns_by_id.get(int(fact["id"]), [])
        if any(str(col.get("name") or "").casefold() == "tagsn" for col in cols):
            return _plan_column_fqn(fact, "tagsn")
    for table_id in selected_ids:
        table = tables_by_id.get(int(table_id))
        if table is None:
            continue
        cols = columns_by_id.get(int(table_id), [])
        if any(str(col.get("name") or "").casefold() == "tagsn" for col in cols):
            return _plan_column_fqn(table, "tagsn")
    for item in planned_filters or []:
        column = str(getattr(item, "column", "") or "")
        tail = column.rsplit(".", 1)[-1].casefold()
        if tail in {"suj_code", "suj_name", "br_code"} and "." in column:
            return column.rsplit(".", 1)[0] + ".tagsn"
    return None


def _table_candidates(tables: list[dict[str, Any]]) -> list:
    return [
        _candidate(table, [], source="schema_rule")
        for table in tables
        if table.get("id") is not None
    ]


def _fact_choice_response(
    analysis,
    reason: str | None,
    tables: list[dict[str, Any]],
    grain: str | None = None,
):
    return fact_unresolved_response(
        analysis,
        reason,
        candidates=_table_candidates(tables),
        candidate_evidence=build_candidate_evidence(
            fact_tables=tables,
            query_requests_fact=True,
            fact_unresolved=reason,
            grain=grain,
        ),
    )


def _merge_table_candidates(
    existing: list,
    tables: list[dict[str, Any]],
) -> list:
    seen = {
        (str(item.schema_name or "").casefold(), str(item.table_name or "").casefold())
        for item in existing
    }
    merged = list(existing)
    for table in tables:
        cand = _candidate(table, [], source="schema_rule")
        key = (
            str(cand.schema_name or "").casefold(),
            str(cand.table_name or "").casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(cand)
    return merged


async def project_range_mappings(
    repository: PostgresMetadataRepository,
    needles: list[str],
    *,
    source_instance_id: str | None = None,
    type_groups: tuple[TypeGroup, ...] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Bind instance-bearing range slots by equality, then one S×T replay."""

    instance_needles = [
        item for item in needles if range_surface_has_instance(item, type_groups)
    ]
    if not instance_needles:
        return [], False
    first = await repository.find_value_mappings(
        instance_needles,
        source_instance_id=source_instance_id,
    )
    bound: list[dict[str, Any]] = []
    unresolved = False
    for needle in instance_needles:
        stage1 = _rows_matching_needle(first, needle)
        unique = unique_code_rows(stage1)
        if unique:
            bound.extend(annotate_matched_mention(unique, needle))
            continue
        if stage1:
            if not _needle_covered_by_bound(needle, bound):
                unresolved = True
            continue
        peeled = peel_type_suffix(needle, type_groups)
        instance = ""
        groups: list[TypeGroup] = []
        if peeled is not None and peeled[0]:
            instance, group = peeled
            groups = [group]
        else:
            instance = SearchMixin._compact_natural_text(needle)
            groups = list(_active_groups(type_groups))
        if not instance:
            if not _needle_covered_by_bound(needle, bound):
                unresolved = True
            continue
        product: list[str] = []
        seen_product: set[str] = set()
        for group in groups:
            for surface in type_product_surfaces(instance, group):
                if surface in seen_product:
                    continue
                seen_product.add(surface)
                product.append(surface)
        if not product:
            if not _needle_covered_by_bound(needle, bound):
                unresolved = True
            continue
        stage2 = await repository.find_value_mappings(
            product,
            source_instance_id=source_instance_id,
        )
        stage2 = [
            row
            for row in stage2
            if any(group.row_in_dictionary(row) for group in groups)
            and any(
                SearchMixin._label_matches_needle(
                    str(row.get("natural_value") or ""),
                    surface,
                )
                for surface in product
            )
        ]
        unique2 = unique_code_rows(stage2)
        codes = {str(row.get("code_value") or "").strip() for row in unique2}
        codes.discard("")
        if unique2 and (peeled is not None or len(codes) == 1):
            bound.extend(annotate_matched_mention(unique2, needle))
        elif not _needle_covered_by_bound(needle, bound):
            unresolved = True
    return bound, unresolved


async def decide(
    repository: PostgresMetadataRepository,
    *,
    query: str,
    include_matched_columns: bool,
    column_top_m: int | None,
    auto_resolve_entities: bool,
    table_limit: int | None = None,
    grain_override: TimeGrain | None = None,
    analysis_timeout_s: float | None = None,
) -> DecisionResponse:
    runtime = get_runtime()
    decision = runtime.decision
    effective_top_k = (
        max(1, min(50, int(table_limit)))
        if table_limit is not None
        else decision.table_top_k
    )
    effective_col_m = (
        max(1, min(50, int(column_top_m)))
        if column_top_m is not None
        else max(1, min(50, int(decision.column_top_m)))
    )

    timeout_raw = analysis_timeout_s
    if timeout_raw is None:
        timeout_raw = getattr(decision, "analysis_timeout_seconds", 15)
    try:
        timeout_s = float(timeout_raw)
    except (TypeError, ValueError):
        timeout_s = 15.0
    started = time.perf_counter()
    logger.warning("decide start")
    if timeout_s < 1:
        analysis = degraded_analysis("파이프라인 시간 부족")
    else:
        analysis = await get_query_analyzer().analyze(query, timeout_s=timeout_s)
    analysis.query = query
    logger.warning(
        "decide analyzed elapsed=%.2f status=%s meaning=%s procedure=%s",
        time.perf_counter() - started,
        analysis.status,
        analysis.meaning_status,
        analysis.procedure,
    )

    if measurement_needs_period(query, analysis):
        return period_required_response(analysis)

    filter_needles = filter_needles_from_analysis(analysis, query)
    range_slots = range_slots_from_analysis(analysis, query)
    range_needles = [
        slot.mention for slot in range_slots if slot.polarity == "include"
    ]
    exclude_needles = [
        slot.mention for slot in range_slots if slot.polarity == "exclude"
    ]
    metric_needles = metric_needles_from_analysis(analysis, query)
    catalog_needles = catalog_needles_from_analysis(analysis, query)
    type_groups = await load_type_groups(repository)
    outputs = list(analysis.primary_outputs or [])
    for slot in range_slots:
        catalog_needles = unique_needles(
            [
                *catalog_needles,
                *[
                    item
                    for item in expand_region_hq_aliases(
                        slot.mention,
                        [slot.mention],
                        groups=type_groups,
                    )
                    if len(compact(item)) >= 4
                ],
            ]
        )
    logger.warning(
        "decide needles elapsed=%.2f filter=%s range=%s metric=%s catalog=%s",
        time.perf_counter() - started,
        filter_needles,
        range_needles,
        metric_needles,
        catalog_needles,
    )

    glossary_rows = await repository.find_glossary_routes(
        unique_needles(
            [
                *filter_needles,
                *metric_needles,
                *[axis_mention(item) for item in outputs],
            ]
        )
    )
    logger.warning(
        "decide glossary elapsed=%.2f n=%s",
        time.perf_counter() - started,
        len(glossary_rows),
    )
    synonym_groups = await load_term_synonym_groups(
        repository,
        unique_needles([*filter_needles, *metric_needles], limit=80),
    )
    mapping_extras = synonym_members_for_needles(synonym_groups, metric_needles)
    if not mapping_extras:
        mapping_extras = glossary_synonyms_for_needles(glossary_rows, metric_needles)
    range_unresolved = False
    range_bound: list[dict[str, Any]] = []
    if meaning_failed(analysis):
        mappings = []
    else:
        include_bound, include_unresolved = await project_range_mappings(
            repository,
            range_needles,
            type_groups=type_groups,
        )
        exclude_bound, exclude_unresolved = await project_range_mappings(
            repository,
            exclude_needles,
            type_groups=type_groups,
        )
        for row in exclude_bound:
            row["filter_polarity"] = "exclude"
        range_bound = [*include_bound, *exclude_bound]
        range_unresolved = include_unresolved or exclude_unresolved
        measure_extras = [
            *mapping_extras,
            *metric_needles,
        ]
        mappings = await repository.find_value_mappings(
            metric_needles,
            extra_mentions=measure_extras,
            trusted_mentions=measure_extras,
        )
        logger.warning(
            "decide mappings elapsed=%.2f range=%s metric=%s extras=%s unresolved=%s",
            time.perf_counter() - started,
            len(range_bound),
            len(mappings),
            mapping_extras,
            range_unresolved,
        )
        prefixes = SearchMixin.prefixes_for_unmatched(
            metric_needles, mapping_labels(mappings)
        )
        if prefixes:
            logger.warning("decide mapping prefixes n=%s", len(prefixes))
            mappings = await repository.find_value_mappings(
                metric_needles,
                extra_mentions=[*measure_extras, *prefixes],
                trusted_mentions=measure_extras,
            )
            logger.warning(
                "decide mappings2 elapsed=%.2f n=%s",
                time.perf_counter() - started,
                len(mappings),
            )
    logger.warning("decide catalog start elapsed=%.2f", time.perf_counter() - started)
    catalog = await repository.find_catalog_by_mentions(catalog_needles)
    logger.warning(
        "decide store elapsed=%.2f filter=%s catalog=%s mappings=%s tables=%s",
        time.perf_counter() - started,
        filter_needles,
        catalog_needles,
        len(mappings),
        len(catalog),
    )
    catalog_mentions = list(catalog)
    selected_source = _provisional_source_instance_id(
        catalog, [*range_bound, *mappings]
    )
    source_dropped_mappings: list[dict[str, Any]] = []
    if selected_source:
        source_dropped_mappings = [
            mapping
            for mapping in [*range_bound, *mappings]
            if not _same_source(mapping, selected_source)
        ]
        range_bound = [
            mapping
            for mapping in range_bound
            if _same_source(mapping, selected_source)
        ]
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
    unique_mappings = [*range_bound, *unique_mappings]
    recall_mappings = embedding_recall_mappings(
        [*unique_mappings, *ambiguous_mappings]
    )
    unique_mappings = approved_code_mappings(unique_mappings)
    held_mappings = approved_code_mappings(ambiguous_mappings)
    seed_ids = seed_table_ids(unique_mappings, catalog)
    group_needles = group_dimension_needles(catalog_needles)
    catalog_column_ids = sorted(
        {
            int(table["id"])
            for table in catalog_mentions
            if table.get("id") is not None
        }
    )
    catalog_columns = (
        await repository.fetch_approved_columns(catalog_column_ids)
        if catalog_column_ids
        else {}
    )
    group_dims = catalog_group_dimensions(
        catalog_mentions,
        group_needles,
        columns_by_id=catalog_columns,
    )
    group_ids = {
        int(table["id"])
        for table in group_dims
        if table.get("id") is not None
    }
    mappings = unique_mappings
    labels = chosen_labels(analysis)
    metric_keys = {
        SearchMixin._compact_natural_text(item)
        for item in metric_needles
        if SearchMixin._compact_natural_text(item)
    }
    if labels and not meaning_failed(analysis):
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
            if SearchMixin._compact_natural_text(label) in metric_keys
            and SearchMixin._compact_natural_text(label)
            and not any(
                SearchMixin._compact_natural_text(label) in item
                or item in SearchMixin._compact_natural_text(label)
                for item in covered
            )
        ]
        if missing_labels:
            remap_groups = await load_term_synonym_groups(
                repository, missing_labels
            )
            remap_extras = [
                *synonym_members_for_needles(remap_groups, missing_labels),
                *glossary_synonyms_for_needles(glossary_rows, missing_labels),
                *missing_labels,
            ]
            remapped = await repository.find_value_mappings(
                missing_labels,
                source_instance_id=selected_source or None,
                extra_mentions=remap_extras,
                trusted_mentions=remap_extras,
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
                held_mappings = [
                    row
                    for row in held_mappings
                    if SearchMixin._compact_natural_text(
                        str(row.get("matched_mention") or "")
                    )
                    not in remapped_mentions
                ]

    extra_recall = embedding_recall_mappings([*mappings, *held_mappings])
    if extra_recall:
        recall_mappings = [*recall_mappings, *extra_recall]
    mappings = approved_code_mappings(mappings)
    held_mappings = approved_code_mappings(held_mappings)

    if not seed_ids and not group_ids:
        if range_unresolved and not range_bound:
            return range_unresolved_response(analysis)
        return empty_meta_response(analysis)

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
        columns_by_id=catalog_columns,
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
    if selected_source:
        list_serving = getattr(repository, "list_serving_tables", None)
        if callable(list_serving):
            serving = await list_serving(selected_source)
            if isinstance(serving, list):
                for table in serving:
                    table_id = table.get("id")
                    if table_id is None:
                        continue
                    if not _same_source(table, selected_source):
                        continue
                    if list_table_type(_resolve_subject_area(table)) not in {
                        "Fact",
                        "Raw",
                    }:
                        continue
                    tables_by_id.setdefault(int(table_id), table)
    parsed_period = parse_period_from_query(
        query,
        str(analysis.period or "").strip() or None,
    )
    week_parsed = parsed_period is not None and parsed_period.week_start is not None
    query_grain = resolve_time_grain(query)
    if grain_override in ("month", "day", "hour", "instant"):
        query_grain = grain_override
    hint = query_grain
    if not hint:
        hint = resolve_time_grain(query, analysis)
    fact_pool = list(tables_by_id.values())
    apply_query_grain = True
    if grain_override in ("month", "day", "hour", "instant"):
        hint = grain_override
    elif week_parsed:
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
    needs_fact = query_requests_fact(query, parsed_period, analysis, mappings)
    if needs_fact:
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
    contested_facts: list[dict[str, Any]] = []
    fact_columns_by_id: dict[int, list[dict[str, Any]]] = {}
    if facts:
        fact_columns_by_id = await repository.fetch_approved_columns(
            sorted(
                {
                    int(table["id"])
                    for table in facts
                    if table.get("id") is not None
                }
            )
        )
    if len(facts) > 1:
        competing = list(facts)
        facts = facts_joinable_to_mappings(
            facts,
            mapped_ids=mapped_ids,
            edges=edges,
            max_hops=hops,
            mappings=mappings,
            fact_columns_by_id=fact_columns_by_id,
        )
        if week_parsed:
            facts = narrow_facts_for_week(facts, query)
        if len(facts) > 1:
            facts = narrow_facts_by_query_clock(facts, query)
        if len(facts) > 1:
            facts = prefer_unique_fact_type(facts)
        if len(facts) > 1 and query_grain is None:
            facts = prefer_day_grain_facts(facts)
        if len(facts) > 1:
            fact_unresolved = fact_unresolved or "팩트 표 미선정"
            contested_facts = list(facts)
            facts = []
        elif len(facts) == 1:
            fact_unresolved = None
        else:
            fact_unresolved = fact_unresolved or "팩트 표 미선정"
            contested_facts = competing
            facts = []
    if mapped_ids or facts or group_ids:
        selected_ids = set(mapped_ids) | group_ids
        for fact in facts:
            selected_ids.add(int(fact["id"]))
    else:
        selected_ids = set(seed_ids)
    selected_ids &= set(tables_by_id)
    stay_on_selected = list_stays_on_selected_tables(
        analysis, has_facts=bool(facts)
    )
    location_tables = location_group_tables(
        list(tables_by_id.values()), query, analysis
    )
    location_ids: set[int] = set()
    for table in location_tables:
        table_id = table.get("id")
        if table_id is not None:
            selected_ids.add(int(table_id))
            location_ids.add(int(table_id))
    if not selected_ids:
        if range_unresolved and not range_bound:
            return range_unresolved_response(analysis)
        if fact_unresolved:
            return _fact_choice_response(
                analysis,
                fact_unresolved,
                contested_facts,
                query_grain or hint,
            )
        return empty_meta_response(analysis)
    probe_ids = set(selected_ids)
    if not stay_on_selected:
        for table in tables_by_id.values():
            table_id = table.get("id")
            if table_id is not None and is_tag_master_table(table):
                probe_ids.add(int(table_id))
    columns = await repository.fetch_approved_columns(sorted(probe_ids))
    mappings = project_code_mappings_to_hub(
        mappings,
        held_mappings,
        query,
        selected_ids=selected_ids,
        tables_by_id=tables_by_id,
        columns_by_id=columns,
        edge_rows=edge_rows,
        edges=edges,
        max_hops=hops,
    )
    mapped_ids = seed_table_ids(mappings, [])
    selected_ids |= mapped_ids & set(tables_by_id)
    catalog_ids = {
        int(table["id"])
        for table in [*catalog, *catalog_mentions]
        if table.get("id") is not None
    }
    before_unjoinable = set(selected_ids)
    selected_ids = drop_unjoinable_catalog_ids(
        selected_ids,
        mapped_ids=mapped_ids,
        fact_ids={int(fact["id"]) for fact in facts},
        catalog_ids=catalog_ids,
        edges=edges,
        max_hops=hops,
        tables_by_id=tables_by_id,
    )
    dropped_unjoinable_ids = before_unjoinable - selected_ids
    selected_ids = drop_unselected_fact_tables(
        selected_ids,
        chosen_fact_ids={int(fact["id"]) for fact in facts},
        tables_by_id=tables_by_id,
        mapped_ids=mapped_ids,
    )
    if not selected_ids:
        if range_unresolved and not range_bound:
            return range_unresolved_response(analysis)
        if fact_unresolved:
            return _fact_choice_response(
                analysis,
                fact_unresolved,
                contested_facts,
                query_grain or hint,
            )
        return empty_meta_response(analysis)
    planned_filters = mapping_filters(mappings, query=query, analysis=analysis)
    rewritten_mapping_ids: set[int] = set()
    if facts:
        planned_filters, rewritten_mapping_ids = rewrite_mapping_filters_onto_facts(
            planned_filters,
            facts=facts,
            fact_columns_by_id=fact_columns_by_id,
            mappings=mappings,
        )
        selected_ids -= rewritten_mapping_ids
        mapped_ids -= rewritten_mapping_ids
    unresolved: list[str] = []
    if range_unresolved and not range_bound:
        unresolved.append(RANGE_CODE_UNRESOLVED)
    if fact_unresolved:
        unresolved.append(fact_unresolved)
    blocked_ids = set(tables_by_id) - selected_ids if stay_on_selected else None
    paths, origin, join_unresolved = assemble_anchor_join_paths(
        selected_ids,
        edges=edges,
        max_hops=hops,
        fact_ids={int(fact["id"]) for fact in facts},
        mapped_ids=mapped_ids,
        tables_by_id=tables_by_id,
        blocked_ids=blocked_ids,
        allow_disconnected=stay_on_selected,
    )
    unresolved.extend(join_unresolved)
    if stay_on_selected:
        origin &= selected_ids
        bridge_ids = set()
    else:
        bridge_ids = origin - selected_ids
        selected_ids |= origin
        bridge_ids = promote_series_identity_tables(
            bridge_ids,
            tables_by_id=tables_by_id,
            query=query,
            needs_fact=needs_fact,
        )

    try:
        selection = await select_from_store(
            query,
            analysis,
            compact_store_recall(
                tables=[
                    tables_by_id[table_id]
                    for table_id in sorted(selected_ids)
                    if table_id in tables_by_id
                ],
                mappings=mappings,
                join_edges=edge_rows,
            ),
            timeout_s=min(15.0, timeout_s),
        )
    except Exception:
        selection = None
    if (
        selection
        and selection.get("accept") is False
        and any("기간" in str(item) for item in (selection.get("missing") or []))
        and measurement_needs_period(query, analysis)
    ):
        return period_required_response(analysis)
    mappings, selected_ids = apply_store_selection(
        selection,
        mappings=mappings,
        selected_ids=selected_ids,
        rewritten_mapping_ids=rewritten_mapping_ids,
    )
    mapped_ids = seed_table_ids(mappings, []) - rewritten_mapping_ids

    columns = await repository.fetch_approved_columns(sorted(selected_ids))
    planned_filters.extend(
        measure_point_label_filters(
            query,
            mappings,
            tables_by_id=tables_by_id,
            columns_by_id=columns,
            table_ids=selected_ids,
            analysis=analysis,
        )
    )
    if facts:
        for fact in facts:
            period = period_filter_for_fact(
                query,
                fact,
                columns.get(int(fact["id"]), []),
                period_text=str(analysis.period or "").strip() or None,
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
    process_rows: list[dict[str, str]] = []
    selected_tables = [
        tables_by_id[table_id]
        for table_id in selected_ids
        if table_id in tables_by_id
    ]
    if (
        source_uses_process_rules([*selected_tables, *facts])
        and not is_catalog_list_query(query, analysis)
        and not meaning_failed(analysis)
        and not bound_tagsn(planned_filters)
    ):
        probe_needles = tag_probe_needles(metric_needles)
        if probe_needles:
            process_rows = await probe_metric_tag_rows(
                filters=planned_filters,
                needles=probe_needles,
                repository=repository,
                source_instance_id=selected_source or None,
            )
            codes = tagsn_codes_from_rows(process_rows)
            column = _tagsn_column_fqn(
                facts, columns, tables_by_id, selected_ids, planned_filters
            )
            if codes and column:
                planned_filters = replace_tagsn_filter(
                    planned_filters,
                    column=column,
                    codes=codes,
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
        join_keys = set(required_columns_by_id[fact_id])
        required_columns_by_id[fact_id].update(
            measure_column_names(columns.get(fact_id, []), exclude=join_keys)
        )
        required_columns_by_id[fact_id].update(
            fact_time_column_names(columns.get(fact_id, []))
        )
    axis = answer_axis_from_analysis(analysis)
    use_list_axis = (
        not needs_fact
        and str(getattr(analysis, "procedure", "") or "").strip() == "list"
        and list_axis_skips_tag_identity(axis)
    )
    for table_id, table in tables_by_id.items():
        if int(table_id) not in selected_ids:
            continue
        typed = list_table_type(_resolve_subject_area(table))
        if typed in {"Fact", "Raw"}:
            continue
        if typed not in {"Dimension", "Code"} and not is_tag_master_table(table):
            continue
        if use_list_axis:
            required_columns_by_id[int(table_id)].update(
                list_axis_identity_column_names(
                    columns.get(int(table_id), []),
                    table,
                    axis,
                )
            )
        else:
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
    if (
        source_uses_process_rules([*selected_tables, *facts])
        and not is_catalog_list_query(query, analysis)
    ):
        if not process_rows and bound_tagsn(planned_filters):
            process_rows = await load_process_rows(
                filters=planned_filters,
                repository=repository,
                source_instance_id=selected_source or None,
            )
        asked = str(getattr(getattr(analysis, "measurement", None), "aggregation", "") or "")
        kept = usage_keep_rows(process_rows, query=query, asked=asked)
        if kept and kept != process_rows:
            process_rows = kept
            codes = tagsn_codes_from_rows(kept)
            column = _tagsn_column_fqn(
                facts, columns, tables_by_id, selected_ids, planned_filters
            )
            if codes and column:
                planned_filters = replace_tagsn_filter(
                    planned_filters,
                    column=column,
                    codes=codes,
                )
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
        time_role=time_role_from_procedure(analysis.procedure),
        answer_axis=answer_axis_from_analysis(analysis),
        aggregation=aggregation_contract(
            analysis=analysis,
            facts=facts,
            columns_by_id=columns,
            period=parsed_period,
            glossary_rows=glossary_rows,
            query=query,
            process_rows=process_rows,
            grain=query_grain or hint,
        ),
        candidate_evidence=build_candidate_evidence(
            selected_mappings=mappings,
            held_mappings=held_mappings,
            source_dropped_mappings=source_dropped_mappings,
            recall_mappings=recall_mappings,
            catalog_tables=catalog,
            fact_tables=[*fact_pool, *tables_by_id.values()],
            selected_ids=selected_ids,
            mapped_ids=mapped_ids,
            group_ids=group_ids,
            location_ids=location_ids,
            chosen_fact_ids={int(fact["id"]) for fact in facts},
            dropped_unjoinable_ids=dropped_unjoinable_ids,
            query_requests_fact=needs_fact,
            stay_on_selected=stay_on_selected,
            fact_unresolved=fact_unresolved,
            grain=query_grain or hint,
        ),
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
    table_ids = [
        int(table["id"])
        for table in ordered_tables[:effective_top_k]
        if table.get("id") is not None
    ]
    code_columns_by_table: dict[int, set[str]] = {}
    if include_matched_columns and table_ids:
        loaded = await repository.find_value_mapping_code_columns(table_ids)
        if isinstance(loaded, dict):
            code_columns_by_table = loaded
    bound_column_names = [
        _column_tail(str(item.column or ""))
        for item in planned_filters
        if str(item.column or "").strip()
    ]
    bound_column_names.extend(
        _column_tail(str(row.get("column_fqn") or row.get("column_name") or ""))
        for row in mappings
    )
    column_needles = unique_needles(
        [
            *filter_needles,
            *catalog_needles,
            *metric_needles,
            *list(analysis.primary_outputs or []),
            *list(analysis.answer_must_include or []),
        ]
    )
    candidates = [
        _candidate(
            table,
            select_question_columns(
                columns.get(int(table["id"]), []),
                required_names=list(required_columns_by_id.get(int(table["id"]), set())),
                bound_names=bound_column_names,
                needles=column_needles,
                limit=effective_col_m,
            )
            if include_matched_columns
            else [],
            source="name_rule" if int(table["id"]) in mapped_ids else "schema_rule",
            code_columns=code_columns_by_table.get(int(table["id"]), set()),
        )
        for table in ordered_tables[:effective_top_k]
    ]
    candidates = _merge_table_candidates(candidates, contested_facts)
    entities = (
        _resolved_entities(
            value_mappings_for_plan(mappings, query, analysis),
            glossary_surfaces=glossary_surfaces(synonym_groups, glossary_rows),
        )
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
    from ..universal_serving import build_universal_plan

    universal_plan = build_universal_plan(
        query=query,
        plan=plan,
        entities=entities,
        candidates=candidates,
        analysis=analysis,
    )
    plan.universal_plan = universal_plan

    return DecisionResponse(
        target=_top_target(candidates),
        candidates=candidates,
        join_groups=join_groups,
        threshold_used={
            "table_top_k": effective_top_k,
            "column_top_m": effective_col_m,
            "fk_max_hops": hops,
        },
        resolved_entities=entities,
        resolution_status="complete" if entities or planned_tables else "failed",
        execution_context=execution_context,
        query_analysis=analysis,
        query_plan=plan,
        glossary_routes=glossary_routes,
        universal_plan=universal_plan,
    )
