"""Store-first seeds, fact pick, and empty-meta response."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Iterable

from ...schemas import (
    DecisionResponse,
    PlanAggregation,
    PlannedFilter,
    QueryAnalysis,
    QueryPlan,
)
from ..decision_planner import CompositeJoinEdge, shortest_path
from ..metadata_repository._search import SearchMixin
from .default_date import _dtype_looks_like_date, _format_looks_like_date
from .filters import (
    _column_looks_like_audit_date,
    _column_looks_like_code,
    _column_looks_like_measure_value,
    _period_bind_value,
)
from .grain import _asks_series, fallback_grains, resolve_time_grain
from .helpers import _metadata_dict, _resolve_subject_area, _serving_logical_name
from .aliases import is_displaced_plant_mapping, peel_type_suffix
from ..meaning_slots import (
    answer_axis_from_analysis,
    axis_mention,
    extremum_function_from_text,
    filter_needles_from_analysis,
    is_answer_axis_text,
    meaning_failed,
    measure_item_surface,
    time_role_from_procedure,
)
from .period import ParsedPeriod, parse_period_from_query, week_mention
from .data_process import FN_IDENTITY, aggregation_tags, refine_function
from .table_type import list_table_type

_FACT_LIKE = frozenset({"Fact", "Raw"})

_MONTH_GRAIN_MARKERS = ("01mm", "월별", "월 단위", "한달")
_STANDALONE_MONTH = re.compile(r"(?<![가-힣])월(?![가-힣])")
_DAY_GRAIN_MARKERS = ("01dd", "일별", "일 단위", "하루")
_STANDALONE_DAY = re.compile(r"(?<![가-힣])일(?![가-힣])")
_HOUR_IN_QUERY = ("시간", "시간별", "매시간")
_EXTREMUM_TOKENS = (
    "최고",
    "가장 높",
    "가장 낮",
    "가장 많",
    "가장 적",
    "최대",
    "최소",
    "제일 높",
    "제일 낮",
    "제일 많",
    "제일 적",
)
_EXTREMUM_LEADS = ("가장", "제일")
_EXTREMUM_TAILS = ("높", "낮", "많", "적")
_EXTREMUM_WINDOW = 16
_FACT_UNRESOLVED = "팩트 표 미선정"
_FACT_GRAIN_UNRESOLVED = "팩트 입도를 스토어 설명과 맞출 수 없음"
RANGE_CODE_UNRESOLVED = "범위 코드 미결합"
PERIOD_REQUIRED = "기간을 지정해 주세요"

_NO_META = "맞는 메타데이터가 없다"

_HINT_WORDS = {
    "month": ("월",),
    "day": ("일",),
    "hour": ("시간",),
    "instant": ("실시간", "순시"),
}


def is_fact_unresolved_reason(text: str) -> bool:
    value = str(text or "")
    return _FACT_UNRESOLVED in value or "팩트 입도" in value


def is_range_code_unresolved_reason(text: str) -> bool:
    return RANGE_CODE_UNRESOLVED in str(text or "")


def is_period_required_reason(text: str) -> bool:
    return PERIOD_REQUIRED in str(text or "")


def period_required_unresolved(plan: QueryPlan | None) -> bool:
    if plan is None:
        return False
    return any(
        is_period_required_reason(str(item))
        for item in (plan.unresolved_requirements or [])
    )


def period_required_response(
    analysis: QueryAnalysis | None = None,
) -> DecisionResponse:
    return DecisionResponse(
        target="none",
        candidates=[],
        threshold_used={},
        resolution_status="complete",
        query_analysis=analysis,
        query_plan=QueryPlan(
            completeness="failed",
            unresolved_requirements=[PERIOD_REQUIRED],
            time_role=time_role_from_procedure(
                str(getattr(analysis, "procedure", "") or "")
            ),
            answer_axis=answer_axis_from_analysis(analysis),
        ),
    )


def range_unresolved_response(
    analysis: QueryAnalysis | None = None,
) -> DecisionResponse:
    return DecisionResponse(
        target="none",
        candidates=[],
        threshold_used={},
        resolution_status="complete",
        query_analysis=analysis,
        query_plan=QueryPlan(
            completeness="partial",
            unresolved_requirements=[RANGE_CODE_UNRESOLVED],
            time_role="none",
        ),
    )


def fact_unresolved_response(
    analysis: QueryAnalysis | None = None,
    reason: str | None = None,
    *,
    candidates: list[Any] | None = None,
    candidate_evidence: list[Any] | None = None,
) -> DecisionResponse:
    return DecisionResponse(
        target="none",
        candidates=list(candidates or []),
        threshold_used={},
        resolution_status="complete",
        query_analysis=analysis,
        query_plan=QueryPlan(
            completeness="partial",
            unresolved_requirements=[reason or _FACT_UNRESOLVED],
            time_role="none",
            candidate_evidence=list(candidate_evidence or []),
        ),
    )


def empty_meta_response(analysis: QueryAnalysis | None = None) -> DecisionResponse:
    return DecisionResponse(
        target="none",
        candidates=[],
        threshold_used={},
        resolution_status="failed",
        query_analysis=analysis,
        query_plan=QueryPlan(
            completeness="failed",
            unresolved_requirements=[_NO_META],
            time_role="none",
        ),
    )


def glossary_extras(rows: list[dict[str, Any]]) -> list[str]:
    extras: list[str] = []
    for row in rows:
        for key in (
            "word_korean",
            "standard_term",
            "mention",
            "surface",
            "abbreviation",
            "english_name",
        ):
            value = str(row.get(key) or "").strip()
            if value:
                extras.append(value)
        aliases = row.get("aliases")
        if isinstance(aliases, list):
            extras.extend(str(item).strip() for item in aliases if str(item).strip())
    return extras


def mapping_labels(mappings: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("natural_value") or "")
        for item in mappings
        if str(item.get("natural_value") or "").strip()
    ]


def _mapping_code_column(mapping: dict[str, Any]) -> str:
    name = str(mapping.get("column_name") or "").strip()
    if name:
        return name
    fqn = str(mapping.get("column_fqn") or "").strip()
    if "." in fqn:
        return fqn.rsplit(".", 1)[-1]
    return fqn


def _mapping_table_id(mapping: dict[str, Any]) -> int | None:
    raw = mapping.get("table_id")
    if raw is None:
        return None
    return int(raw)


def _code_tables_are_linked(
    rows: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]],
) -> bool:
    ids = {
        table_id
        for table_id in (_mapping_table_id(row) for row in rows)
        if table_id is not None
    }
    if len(ids) < 2:
        return False
    for edge in edge_rows:
        left = int(edge.get("from_table_id") or 0)
        right = int(edge.get("to_table_id") or 0)
        if left in ids and right in ids:
            return True
    return False


def _fk_parent_table_ids(
    rows: list[dict[str, Any]],
    edge_rows: list[dict[str, Any]],
) -> set[int]:
    ids = {
        table_id
        for table_id in (_mapping_table_id(row) for row in rows)
        if table_id is not None
    }
    parents: set[int] = set()
    for edge in edge_rows:
        src = int(edge.get("from_table_id") or 0)
        dst = int(edge.get("to_table_id") or 0)
        if src in ids and dst in ids:
            parents.add(dst)
    return parents


def _child_specific_rows(
    query: str,
    mention: str,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    mention_c = SearchMixin._compact_natural_text(mention)
    hits: list[dict[str, Any]] = []
    for row in rows:
        label = SearchMixin._compact_natural_text(str(row.get("natural_value") or ""))
        if len(label) <= len(mention_c):
            continue
        if mention_c and mention_c not in label:
            continue
        if SearchMixin._natural_value_is_standalone_mention(
            query,
            str(row.get("natural_value") or ""),
        ):
            hits.append(row)
    return hits


def _measurement_hub_column_names(
    *,
    selected_ids: set[int],
    tables_by_id: dict[int, dict[str, Any]],
    columns_by_id: dict[int, list[dict[str, Any]]],
    edges: list[CompositeJoinEdge],
    max_hops: int,
) -> set[str]:
    fact_ids = {
        int(table_id)
        for table_id, table in tables_by_id.items()
        if int(table_id) in selected_ids
        and list_table_type(_resolve_subject_area(table)) in _FACT_LIKE
    }
    hub_ids: set[int] = set(fact_ids)
    for table_id, table in tables_by_id.items():
        tid = int(table_id)
        if not is_tag_master_table(table):
            continue
        if tid in selected_ids:
            hub_ids.add(tid)
            continue
        if fact_ids and shortest_path(
            edges,
            source_ids=fact_ids,
            target_id=tid,
            max_hops=max_hops,
        ) is not None:
            hub_ids.add(tid)
    names: set[str] = set()
    for table_id in hub_ids:
        for column in columns_by_id.get(int(table_id), []):
            name = str(column.get("name") or "").strip()
            if name:
                names.add(name.casefold())
    return names


def project_code_mappings_to_hub(
    kept: list[dict[str, Any]],
    held: list[dict[str, Any]],
    query: str,
    *,
    selected_ids: set[int],
    tables_by_id: dict[int, dict[str, Any]],
    columns_by_id: dict[int, list[dict[str, Any]]],
    edge_rows: list[dict[str, Any]],
    edges: list[CompositeJoinEdge],
    max_hops: int,
) -> list[dict[str, Any]]:
    """Keep parent/child dictionary hits that the fact/tag hub can filter on.

    Same mention on a parent code table and a child composite table is not
    ambiguity. Prefer the code column that exists on the measurement hub.
    """

    grouped: dict[str, list[dict[str, Any]]] = {}
    leftover: list[dict[str, Any]] = []
    for row in [*kept, *held]:
        token = SearchMixin._compact_natural_text(str(row.get("matched_mention") or ""))
        if not token:
            leftover.append(row)
            continue
        grouped.setdefault(token, []).append(row)
    hub_cols = _measurement_hub_column_names(
        selected_ids=selected_ids,
        tables_by_id=tables_by_id,
        columns_by_id=columns_by_id,
        edges=edges,
        max_hops=max_hops,
    )
    admitted: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def _take(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            key = (
                SearchMixin._compact_natural_text(str(row.get("matched_mention") or "")),
                str(row.get("column_fqn") or "").casefold(),
                str(row.get("code_value") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            admitted.append(row)

    kept_ids = {id(row) for row in kept}
    for mention, rows in grouped.items():
        columns = {
            str(row.get("column_fqn") or "").strip().casefold()
            for row in rows
            if str(row.get("column_fqn") or "").strip()
        }
        if len(columns) <= 1 or not _code_tables_are_linked(rows, edge_rows):
            _take([row for row in rows if id(row) in kept_ids])
            continue
        on_hub = [
            row
            for row in rows
            if _mapping_code_column(row).casefold() in hub_cols
        ]
        specific = _child_specific_rows(query, mention, rows)
        if specific:
            specific_hub = [
                row
                for row in specific
                if _mapping_code_column(row).casefold() in hub_cols
            ]
            _take(specific_hub or specific)
            continue
        parents = _fk_parent_table_ids(rows, edge_rows)
        if on_hub:
            parent_on_hub = [
                row
                for row in on_hub
                if _mapping_table_id(row) in parents
            ]
            _take(parent_on_hub or on_hub)
            continue
        parent_rows = [
            row
            for row in rows
            if _mapping_table_id(row) in parents
        ]
        _take(parent_rows)
    _take(leftover)
    return admitted


def filter_mappings_to_labels(
    mappings: list[dict[str, Any]],
    labels: list[str],
) -> list[dict[str, Any]]:
    wanted = {SearchMixin._compact_natural_text(item) for item in labels if item}
    if not wanted:
        return mappings
    kept: list[dict[str, Any]] = []
    for mapping in mappings:
        natural = SearchMixin._compact_natural_text(
            str(mapping.get("natural_value") or "")
        )
        if any(
            SearchMixin._label_starts_with(natural, label)
            or SearchMixin._label_starts_with(label, natural)
            for label in wanted
        ):
            kept.append(mapping)
    unique, _ambiguous = partition_mention_mappings(kept)
    return unique


def chosen_labels(analysis: QueryAnalysis) -> list[str]:
    return filter_needles_from_analysis(analysis)


def partition_mention_mappings(
    mappings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep unique mention→label hits. Ambiguous prefix groups wait for analyze."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    unique: list[dict[str, Any]] = []
    for mapping in mappings:
        token = SearchMixin._compact_natural_text(
            str(mapping.get("matched_mention") or "")
        )
        if not token:
            unique.append(mapping)
            continue
        grouped.setdefault(token, []).append(mapping)
    ambiguous: list[dict[str, Any]] = []
    for token, rows in grouped.items():
        labels = {
            SearchMixin._compact_natural_text(str(row.get("natural_value") or ""))
            for row in rows
            if str(row.get("natural_value") or "").strip()
        }
        if len(labels) <= 1:
            unique.extend(rows)
            continue
        columns = {
            str(row.get("column_fqn") or "").strip().casefold()
            for row in rows
            if str(row.get("column_fqn") or "").strip()
        }
        if len(columns) == 1 and len(token) >= 3:
            unique.extend(rows)
        else:
            ambiguous.extend(rows)
    return unique, ambiguous


def seed_table_ids(
    mappings: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
) -> set[int]:
    ids: set[int] = set()
    for mapping in mappings:
        if mapping.get("table_id") is not None:
            ids.add(int(mapping["table_id"]))
    for table in catalog:
        if table.get("id") is not None:
            ids.add(int(table["id"]))
    return ids


def table_blob(table: dict[str, Any]) -> str:
    return " ".join(
        [
            str(_serving_logical_name(table) or ""),
            str(table.get("description") or ""),
            str(table.get("analyzed_description") or ""),
        ]
    )


def pick_fact_tables(
    tables: list[dict[str, Any]],
    hint: str | None,
    query: str = "",
    *,
    apply_query_grain: bool = True,
) -> tuple[list[dict[str, Any]], str | None]:
    facts = [
        table
        for table in tables
        if list_table_type(_resolve_subject_area(table)) in {"Fact", "Raw"}
    ]
    if not facts:
        return [], None
    grain = str(hint or "").strip().lower() or None
    if apply_query_grain and grain not in _HINT_WORDS:
        grain = resolve_time_grain(query) if query else None
    elif grain not in _HINT_WORDS:
        grain = None
    words = _HINT_WORDS.get(str(grain or ""), ())
    if words:
        matched = [
            table
            for table in facts
            if any(word in table_blob(table) for word in words)
        ]
        if len(matched) == 1:
            return matched, None
        if len(matched) > 1:
            return matched, _FACT_UNRESOLVED
        for finer in fallback_grains(grain):
            finer_words = _HINT_WORDS.get(str(finer), ())
            if not finer_words:
                continue
            finer_matched = [
                table
                for table in facts
                if any(word in table_blob(table) for word in finer_words)
            ]
            if len(finer_matched) == 1:
                return finer_matched, None
            if len(finer_matched) > 1:
                return finer_matched, _FACT_UNRESOLVED
        return [], _FACT_GRAIN_UNRESOLVED
    if query:
        scored: list[tuple[int, dict[str, Any]]] = []
        for table in facts:
            blob = table_blob(table)
            score = sum(
                1
                for word in ("월", "일", "시간", "실시간", "순시")
                if word in query and word in blob
            )
            scored.append((score, table))
        best = max(item[0] for item in scored)
        if best > 0:
            winners = [table for score, table in scored if score == best]
            if len(winners) == 1:
                return winners, None
    preferred = [
        table
        for table in facts
        if list_table_type(_resolve_subject_area(table)) == "Fact"
    ]
    chosen = preferred or facts
    if len(chosen) == 1:
        return chosen, None
    if len(chosen) > 1:
        day_only = prefer_day_grain_facts(chosen)
        if len(day_only) == 1:
            return day_only, None
        if len(day_only) > 1:
            return day_only, _FACT_UNRESOLVED
        return chosen, _FACT_UNRESOLVED
    return chosen, None


def is_month_grain_table(table: dict[str, Any]) -> bool:
    blob = table_blob(table)
    compact = SearchMixin._compact_natural_text(blob)
    if any(marker in blob or marker in compact for marker in _MONTH_GRAIN_MARKERS):
        return True
    return bool(_STANDALONE_MONTH.search(blob))


def is_day_grain_table(table: dict[str, Any]) -> bool:
    blob = table_blob(table)
    compact = SearchMixin._compact_natural_text(blob)
    if any(marker in blob or marker in compact for marker in _DAY_GRAIN_MARKERS):
        return True
    return bool(_STANDALONE_DAY.search(blob))


def facts_joinable_to_mappings(
    facts: list[dict[str, Any]],
    *,
    mapped_ids: set[int],
    edges: list[Any],
    max_hops: int,
    mappings: list[dict[str, Any]] | None = None,
    fact_columns_by_id: dict[int, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Keep facts with approved FK paths, else same code-column names. Do not mix scores."""

    if not mapped_ids or not facts:
        return list(facts)
    fk_scored: list[tuple[int, dict[str, Any]]] = []
    for fact in facts:
        fact_id = int(fact["id"])
        hits = 0
        for mapped_id in mapped_ids:
            if int(mapped_id) == fact_id:
                hits += 1
                continue
            if shortest_path(
                edges,
                source_ids={int(mapped_id)},
                target_id=fact_id,
                max_hops=max_hops,
            ) is not None:
                hits += 1
        if hits:
            fk_scored.append((hits, fact))
    if fk_scored:
        best = max(item[0] for item in fk_scored)
        return [fact for hits, fact in fk_scored if hits == best]
    code_names = {
        _mapping_code_column(row).casefold()
        for row in (mappings or [])
        if _mapping_code_column(row)
    }
    if not code_names or not fact_columns_by_id:
        return []
    named: list[dict[str, Any]] = []
    for fact in facts:
        columns = fact_columns_by_id.get(int(fact["id"]), [])
        col_names = {
            str(column.get("name") or column.get("column_name") or "")
            .strip()
            .casefold()
            for column in columns
            if str(column.get("name") or column.get("column_name") or "").strip()
        }
        if code_names & col_names:
            named.append(fact)
    return named


def is_hour_grain_table(table: dict[str, Any]) -> bool:
    blob = table_blob(table)
    compact = SearchMixin._compact_natural_text(blob)
    if any(marker in blob or marker in compact for marker in ("01hh", "시간별", "시간 단위", "매시간")):
        return True
    return "시간" in blob


def narrow_facts_by_query_clock(
    facts: list[dict[str, Any]],
    query: str,
) -> list[dict[str, Any]]:
    if not facts:
        return []
    if not any(token in (query or "") for token in _HOUR_IN_QUERY):
        return list(facts)
    hour_only = [table for table in facts if is_hour_grain_table(table)]
    if hour_only:
        return hour_only
    return list(facts)


def prefer_unique_fact_type(
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(facts) <= 1:
        return list(facts)
    preferred = [
        table
        for table in facts
        if list_table_type(_resolve_subject_area(table)) == "Fact"
    ]
    if len(preferred) == 1:
        return preferred
    return list(facts)


def prefer_day_grain_facts(
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """When the question did not lock a grain, keep day-grain facts."""

    if len(facts) <= 1:
        return list(facts)
    day_only = [table for table in facts if is_day_grain_table(table)]
    if day_only:
        return day_only
    return list(facts)


def group_dimension_needles(
    needles: list[str],
    query: str = "",
    extras: list[str] | None = None,
) -> list[str]:
    """Catalog needles only. Glossary extras are not group seeds."""

    del extras
    kept: list[str] = []
    seen: set[str] = set()
    for token in needles:
        compact = SearchMixin._compact_natural_text(token)
        if len(compact) < 2 or compact in seen:
            continue
        seen.add(compact)
        kept.append(token)
    return kept


def catalog_group_dimensions(
    catalog: list[dict[str, Any]],
    tokens: list[str],
    columns_by_id: dict[int, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Keep mention-matched Dimension tables as group-by seeds. No invented names."""

    needles = SearchMixin.expand_plant_mention_tokens(
        [
            SearchMixin._compact_natural_text(token)
            for token in tokens
            if len(SearchMixin._compact_natural_text(token)) >= 2
        ]
    )
    kept: list[dict[str, Any]] = []
    seen: set[int] = set()
    for table in catalog:
        if list_table_type(_resolve_subject_area(table)) not in {"Dimension", "Code"}:
            continue
        table_id = table.get("id")
        if table_id is None or int(table_id) in seen:
            continue
        blobs = [
            SearchMixin._compact_natural_text(
                " ".join(
                    [
                        table_blob(table),
                        str(table.get("original_name") or ""),
                        str(table.get("name") or ""),
                    ]
                )
            )
        ]
        for column in (columns_by_id or {}).get(int(table_id), []):
            blobs.append(_column_identity_blob(column))
            name = str(column.get("name") or column.get("column_name") or "").strip()
            if name:
                blobs.append(SearchMixin._compact_natural_text(name))
        if any(
            blob
            and needle
            and (
                SearchMixin._label_matches_needle(blob, needle)
                or SearchMixin._label_starts_with(blob, needle)
                or blob.endswith(SearchMixin._compact_natural_text(needle))
            )
            for blob in blobs
            for needle in needles
        ):
            seen.add(int(table_id))
            kept.append(table)
    return kept


_LOCATION_QUERY_TOKENS = ("어디", "곳이")
_LOCATION_TABLE_MARKERS = ("사업장", "정수장", "측정위치")


def location_group_tables(
    tables: list[dict[str, Any]],
    query: str,
    analysis: QueryAnalysis | None = None,
) -> list[dict[str, Any]]:
    """Keep store location masters when the answer axis is a place."""

    outputs = answer_axis_from_analysis(analysis) if analysis else []
    target = str(getattr(analysis, "target", "") or "") if analysis else ""
    procedure = str(getattr(analysis, "procedure", "") or "") if analysis else ""
    axis_blob = SearchMixin._compact_natural_text(
        " ".join([*outputs, target, procedure])
    )
    query_hit = any(token in (query or "") for token in _LOCATION_QUERY_TOKENS)
    axis_hit = any(marker in axis_blob for marker in _LOCATION_TABLE_MARKERS)
    if not query_hit and not axis_hit:
        return []
    kept: list[dict[str, Any]] = []
    seen: set[int] = set()
    for table in tables:
        if list_table_type(_resolve_subject_area(table)) not in {"Dimension", "Code"}:
            continue
        table_id = table.get("id")
        if table_id is None or int(table_id) in seen:
            continue
        blob = SearchMixin._compact_natural_text(
            " ".join(
                [
                    table_blob(table),
                    str(table.get("original_name") or ""),
                    str(table.get("name") or ""),
                ]
            )
        )
        if any(marker in blob for marker in _LOCATION_TABLE_MARKERS):
            seen.add(int(table_id))
            kept.append(table)
    return kept


def narrow_facts_for_week(
    facts: list[dict[str, Any]],
    query: str,
) -> list[dict[str, Any]]:
    """Week without a clock word is a day period. Do not invent a table name."""

    if not facts:
        return []
    if any(token in (query or "") for token in _HOUR_IN_QUERY):
        return list(facts)
    day_only = [table for table in facts if is_day_grain_table(table)]
    if day_only:
        return day_only
    return list(facts)


def drop_unjoinable_catalog_ids(
    selected_ids: set[int],
    *,
    mapped_ids: set[int],
    fact_ids: set[int],
    catalog_ids: set[int],
    edges: list[Any],
    max_hops: int,
    tables_by_id: dict[int, dict[str, Any]] | None = None,
) -> set[int]:
    catalog_only = (catalog_ids & selected_ids) - mapped_ids - fact_ids
    kept = set(selected_ids)
    if not fact_ids:
        for catalog_id in catalog_only:
            table = (tables_by_id or {}).get(int(catalog_id)) or {}
            if list_table_type(_resolve_subject_area(table)) in {"Dimension", "Code"}:
                continue
            kept.discard(catalog_id)
        return kept
    for catalog_id in catalog_only:
        table = (tables_by_id or {}).get(int(catalog_id)) or {}
        if list_table_type(_resolve_subject_area(table)) in {"Dimension", "Code"}:
            continue
        reachable = any(
            shortest_path(
                edges,
                source_ids={int(catalog_id)},
                target_id=int(fact_id),
                max_hops=max_hops,
            )
            is not None
            for fact_id in fact_ids
        )
        if not reachable:
            kept.discard(catalog_id)
    return kept


def drop_unselected_fact_tables(
    selected_ids: set[int],
    *,
    chosen_fact_ids: set[int],
    tables_by_id: dict[int, dict[str, Any]],
    mapped_ids: set[int] | None = None,
) -> set[int]:
    """Unchosen Fact/Raw is not required. Keep mapping/dimension seeds."""

    if chosen_fact_ids:
        return set(selected_ids)
    keep_mapped = mapped_ids or set()
    kept = set(selected_ids)
    for table_id in list(kept):
        if int(table_id) in keep_mapped:
            continue
        table = tables_by_id.get(int(table_id)) or {}
        if list_table_type(_resolve_subject_area(table)) in _FACT_LIKE:
            kept.discard(table_id)
    return kept


def assemble_anchor_join_paths(
    selected_ids: set[int],
    *,
    edges: list[CompositeJoinEdge],
    max_hops: int,
    fact_ids: set[int],
    mapped_ids: set[int] | None = None,
    tables_by_id: dict[int, dict[str, Any]] | None = None,
    blocked_ids: set[int] | None = None,
    allow_disconnected: bool = False,
) -> tuple[list[list[CompositeJoinEdge]], set[int], list[str]]:
    """Reach each required table from measurement/mapping anchors.

    Parent dimensions already on the path are not search origins. A place
    master must attach through the fact/tag identity, not a region parent.
    List without a fact stays on already selected tables; unselected hops
    are not pulled into the plan.
    """

    blocked = {int(table_id) for table_id in (blocked_ids or set())}
    selected = {int(table_id) for table_id in selected_ids} - blocked
    if not selected:
        return [], set(), []
    facts = {int(table_id) for table_id in fact_ids if int(table_id) in selected}
    mapped = {
        int(table_id)
        for table_id in (mapped_ids or set())
        if int(table_id) in selected
    }
    anchors = facts or mapped or {min(selected)}
    connected = set(anchors)
    paths: list[list[CompositeJoinEdge]] = []
    unresolved: list[str] = []
    for table_id in sorted(selected - anchors):
        path = shortest_path(
            edges,
            source_ids=anchors,
            target_id=table_id,
            max_hops=max_hops,
            blocked_ids=blocked,
        )
        if path is None:
            if allow_disconnected:
                connected.add(table_id)
                continue
            table = (tables_by_id or {}).get(table_id) or {}
            label = table.get("original_name") or table.get("name") or table_id
            unresolved.append(f"승인 JOIN 경로 없음: {label}")
            continue
        if path:
            paths.append(path)
            for edge in path:
                connected.add(int(edge.left_table_id))
                connected.add(int(edge.right_table_id))
        connected.add(table_id)
    return paths, connected - blocked, unresolved


def list_stays_on_selected_tables(
    analysis: QueryAnalysis | None,
    *,
    has_facts: bool,
) -> bool:
    """A list with no fact does not admit unselected join hops."""

    if has_facts:
        return False
    return str(getattr(analysis, "procedure", "") or "").strip() == "list"


def join_path_left_unresolved(plan: QueryPlan | None) -> bool:
    if plan is None:
        return False
    return any(
        "승인 JOIN 경로 없음" in str(item)
        for item in (plan.unresolved_requirements or [])
    )


def asks_extremum(query: str) -> bool:
    """Peak language. Allow a short span between 가장/제일 and 높/낮/많/적."""

    text = query or ""
    if any(token in text for token in _EXTREMUM_TOKENS):
        return True
    compact = SearchMixin._compact_natural_text(text)
    for lead in _EXTREMUM_LEADS:
        start = 0
        while True:
            index = compact.find(lead, start)
            if index < 0:
                break
            rest = compact[index + len(lead) : index + len(lead) + _EXTREMUM_WINDOW]
            if any(tail in rest for tail in _EXTREMUM_TAILS):
                return True
            start = index + len(lead)
    return False


def resolve_time_role(
    query: str = "",
    period: ParsedPeriod | None = None,
    *,
    procedure: str = "",
) -> str:
    del query, period
    return time_role_from_procedure(procedure)


def unbound_period_filter(period: ParsedPeriod) -> PlannedFilter:
    if period.week_start is not None and period.week_end is not None:
        value = f"{period.week_start.isoformat()},{period.week_end.isoformat()}"
        operator = "BETWEEN"
    else:
        value = f"{period.like_prefix}%"
        operator = "LIKE"
    return PlannedFilter(
        meaning="측정 기간",
        column=None,
        operator=operator,  # type: ignore[arg-type]
        value=value,
        resolution_status="unresolved",
    )


def period_filter_for_fact(
    query: str,
    fact: dict[str, Any],
    columns: list[dict[str, Any]],
    period_text: str | None = None,
) -> PlannedFilter | None:
    period = parse_period_from_query(query, period_text)
    if period is None:
        return None
    dated = [
        column
        for column in columns
        if _column_is_store_date(column) and not _column_looks_like_audit_date(column)
    ]
    if not dated:
        if period.week_start is not None:
            return unbound_period_filter(period)
        return PlannedFilter(
            meaning="측정 기간",
            operator="LIKE",
            resolution_status="unresolved",
        )
    column = dated[0]
    operator, value = _period_bind_value(column, period)
    schema = str(fact.get("schema_name") or "")
    table = str(fact.get("original_name") or fact.get("name") or "")
    name = str(column.get("name") or "")
    return PlannedFilter(
        meaning="측정 기간",
        column=f"{schema}.{table}.{name}" if schema and table and name else name,
        operator=operator,  # type: ignore[arg-type]
        value=value,
        resolution_status="resolved",
        confidence=1.0,
    )


def _column_is_identity_or_code(column: dict[str, Any] | None) -> bool:
    """Join/PK/FK and code-like columns are identifiers, not measured values."""

    if not column:
        return False
    if column.get("is_primary_key") or column.get("is_foreign_key"):
        return True
    if _column_looks_like_code(column):
        return True
    blob = _column_identity_blob(column)
    return any(marker in blob for marker in ("코드", "식별"))


def measure_column_names(
    columns: list[dict[str, Any]],
    exclude: Iterable[str] | None = None,
) -> list[str]:
    """Numeric value columns. Join keys, PK/FK, and code/identity columns are not measures."""

    skipped = {str(item).strip() for item in (exclude or []) if str(item).strip()}
    names: list[str] = []
    for column in columns:
        if _column_is_store_date(column) or _column_looks_like_audit_date(column):
            continue
        if _column_is_identity_or_code(column):
            continue
        dtype = str(column.get("dtype") or column.get("data_type") or "").casefold()
        if not any(
            token in dtype
            for token in ("int", "numeric", "decimal", "float", "double", "real", "number")
        ):
            continue
        name = str(column.get("name") or "").strip()
        if name and name not in skipped:
            names.append(name)
    return names


def _column_dtype_text(column: dict[str, Any]) -> str:
    return str(column.get("dtype") or column.get("data_type") or "").casefold()


def _column_is_integral(column: dict[str, Any]) -> bool:
    dtype = _column_dtype_text(column)
    if any(token in dtype for token in ("numeric", "decimal", "float", "double", "real")):
        return False
    return "int" in dtype


def column_declared_weight(
    column: dict[str, Any],
    glossary_rows: list[dict[str, Any]] | None = None,
) -> bool:
    """Weight only when column meta or glossary says 가중. Count-like names are not enough."""

    blob = _column_identity_blob(column)
    if "가중" in blob:
        return True
    name = SearchMixin._compact_natural_text(str(column.get("name") or ""))
    logical = SearchMixin._compact_natural_text(
        str((_metadata_dict(column.get("metadata")).get("column_name_kr") or ""))
    )
    for row in glossary_rows or []:
        surfaces = " ".join(
            str(row.get(key) or "")
            for key in ("standard_term", "word_korean", "mention", "surface")
        )
        if "가중" not in SearchMixin._compact_natural_text(surfaces):
            continue
        if name and name in SearchMixin._compact_natural_text(surfaces):
            return True
        if logical and logical in SearchMixin._compact_natural_text(surfaces):
            return True
    return False


def _time_scope_text(period: ParsedPeriod) -> str:
    if period.week_start is not None and period.week_end is not None:
        return f"{period.week_start.isoformat()}/{period.week_end.isoformat()}"
    start = period.start_date()
    end = period.end_date_exclusive() - timedelta(days=1)
    return f"{start.isoformat()}/{end.isoformat()}"


def aggregation_contract(
    *,
    analysis: QueryAnalysis | None,
    facts: list[dict[str, Any]],
    columns_by_id: dict[int, list[dict[str, Any]]],
    period: ParsedPeriod | None,
    glossary_rows: list[dict[str, Any]] | None = None,
    query: str = "",
    process_rows: list[dict[str, str]] | None = None,
    grain: str | None = None,
) -> PlanAggregation | None:
    procedure = str(getattr(analysis, "procedure", "") or "").strip() if analysis else ""
    if procedure == "list" or is_catalog_list_query(query, analysis):
        return None
    if procedure == "lookup" and not process_rows:
        return None
    if procedure not in {"aggregate", "extremum", "lookup"}:
        return None
    function = ""
    if analysis is not None:
        function = str(analysis.measurement.aggregation or "").strip().upper()
    asked = extremum_function_from_text(
        " ".join(
            [
                query,
                str(getattr(analysis, "intent", "") or "") if analysis else "",
                str(getattr(analysis, "goal", "") or "") if analysis else "",
                str(getattr(analysis, "procedure_why", "") or "") if analysis else "",
            ]
        )
    )
    if procedure == "extremum":
        function = asked or function or "MAX"
    elif procedure == "lookup" and not function:
        function = FN_IDENTITY
    elif not function:
        function = "AVG"
    value_column = None
    weight_column = None
    for fact in facts:
        columns = columns_by_id.get(int(fact["id"]), [])
        measure = [
            column
            for column in columns
            if str(column.get("name") or "").strip()
            in set(measure_column_names(columns))
        ]
        weights = [
            column for column in measure if column_declared_weight(column, glossary_rows)
        ]
        values = [column for column in measure if column not in weights]
        if weight_column is None and weights:
            weight_column = str(weights[0].get("name") or "").strip() or None
        if value_column is None and values:
            def _rank(column: dict[str, Any]) -> tuple[int, int, int, int]:
                blob = _column_identity_blob(column)
                identity = int(_column_is_identity_or_code(column))
                countish = int(
                    any(token in blob for token in ("건수", "횟수"))
                )
                valueish = int(
                    any(token in blob for token in ("측정값", "수치"))
                    or _column_looks_like_measure_value(column)
                )
                return (
                    identity,
                    countish,
                    int(_column_is_integral(column)),
                    1 - valueish,
                )

            chosen = sorted(values, key=_rank)[0]
            value_column = str(chosen.get("name") or "").strip() or None
        if value_column is not None:
            break
    weighted = bool(weight_column)
    tag_combine = None
    if process_rows:
        function, tag_combine = refine_function(
            asked=function,
            procedure=procedure,
            rows=process_rows,
            grain=grain,
            query=query,
        )
    return PlanAggregation(
        function=function,
        value_column=value_column,
        weighted=weighted,
        weight_column=weight_column,
        time_scope=_time_scope_text(period) if period is not None else None,
        tag_combine=tag_combine,
        tags=aggregation_tags(
            process_rows,
            function=function,
            tag_combine=tag_combine,
        ),
    )


_LABEL_MARKERS = ("명칭", "이름")
_TAG_LABEL_MARKERS = ("설명",)
_CODE_MARKERS = ("코드", "식별")
_IDENTITY_SKIP = ("설명", "비고", "구분", "여부")
_TAG_IDENTITY_SKIP = (
    "비고",
    "구분",
    "여부",
    "주소",
    "경로",
    "사이트",
    "계산",
    "함수",
    "시설",
    "지자체",
    "보고",
    "사무소",
    "광역",
    "사업장",
    "유역",
    "변량",
    "별칭",
    "명칭",
    "이름",
)
_IDENTITY_RELATIONAL = ("광역", "사무소", "소재지", "배열", "순서")


def is_tag_master_table(table: dict[str, Any] | None) -> bool:
    """A measurement-point catalog. Not a fact, not a physical table name."""

    if not table:
        return False
    if list_table_type(_resolve_subject_area(table)) in _FACT_LIKE:
        return False
    logical = SearchMixin._compact_natural_text(_serving_logical_name(table) or "")
    blob = SearchMixin._compact_natural_text(table_blob(table))
    text = f"{logical} {blob}"
    if "태그마스터" in text:
        return True
    return bool(logical) and "태그" in logical


def promote_series_identity_tables(
    bridge_ids: set[int],
    *,
    tables_by_id: dict[int, dict[str, Any]],
    query: str,
    needs_fact: bool = False,
) -> set[int]:
    """Fact/series answers keep the tag catalog in SELECT, not as a join-only hop."""

    kept = {int(table_id) for table_id in bridge_ids}
    if not needs_fact and not _asks_series(query):
        return kept
    for table_id in list(kept):
        if is_tag_master_table(tables_by_id.get(table_id)):
            kept.discard(table_id)
    return kept


def _column_identity_blob(column: dict[str, Any]) -> str:
    metadata = _metadata_dict(column.get("metadata"))
    return SearchMixin._compact_natural_text(
        " ".join(
            [
                str(metadata.get("column_name_kr") or ""),
                str(metadata.get("logical_name") or ""),
                str(column.get("description") or ""),
                str(column.get("analyzed_description") or ""),
            ]
        )
    )


def dimension_identity_column_names(
    columns: list[dict[str, Any]],
    table: dict[str, Any] | None = None,
) -> list[str]:
    """Store label/code columns for a Dimension/Code master. Do not invent names."""

    tag_master = is_tag_master_table(table)
    skip = _TAG_IDENTITY_SKIP if tag_master else _IDENTITY_SKIP
    labels = _TAG_LABEL_MARKERS if tag_master else _LABEL_MARKERS
    names: list[str] = []
    seen: set[str] = set()
    for column in columns:
        name = str(column.get("name") or "").strip()
        if not name or name in seen:
            continue
        if _column_looks_like_audit_date(column) or _column_is_store_date(column):
            continue
        if column.get("is_primary_key"):
            seen.add(name)
            names.append(name)
            continue
        blob = _column_identity_blob(column)
        if any(token in blob for token in skip):
            continue
        relational = any(token in blob for token in _IDENTITY_RELATIONAL)
        is_label = any(marker in blob for marker in labels)
        is_code = False
        if not tag_master and not relational:
            is_code = any(marker in blob for marker in _CODE_MARKERS)
        if not is_label and not is_code:
            continue
        seen.add(name)
        names.append(name)
    return names


def _plan_column_fqn(table: dict[str, Any], column_name: str) -> str:
    schema = str(table.get("schema_name") or "")
    table_name = str(table.get("original_name") or table.get("name") or "")
    name = str(column_name or "").strip()
    if schema and table_name and name:
        return f"{schema}.{table_name}.{name}"
    return name


def _filter_last_ident(column_fqn: str) -> str:
    text = str(column_fqn or "").strip()
    if "." in text:
        return text.rsplit(".", 1)[-1]
    return text


def rewrite_mapping_filters_onto_facts(
    planned: list[PlannedFilter],
    *,
    facts: list[dict[str, Any]],
    fact_columns_by_id: dict[int, list[dict[str, Any]]],
    mappings: list[dict[str, Any]],
) -> tuple[list[PlannedFilter], set[int]]:
    """Move mapping filters onto a chosen fact when the last ident exists there."""

    if not planned or not facts:
        return list(planned), set()
    name_by_fact: dict[int, dict[str, str]] = {}
    for fact in facts:
        fact_id = int(fact["id"])
        names: dict[str, str] = {}
        for column in fact_columns_by_id.get(fact_id, []):
            name = str(column.get("name") or column.get("column_name") or "").strip()
            if name:
                names[name.casefold()] = name
        name_by_fact[fact_id] = names
    mapping_by_fqn: dict[str, set[int]] = {}
    for row in mappings:
        fqn = str(row.get("column_fqn") or "").strip()
        table_id = _mapping_table_id(row)
        if not fqn or table_id is None:
            continue
        mapping_by_fqn.setdefault(fqn.casefold(), set()).add(table_id)
    rewritten: list[PlannedFilter] = []
    table_ok: dict[int, bool] = {}
    for item in planned:
        source_ids = mapping_by_fqn.get(str(item.column or "").strip().casefold(), set())
        last = _filter_last_ident(item.column or "").casefold()
        target: str | None = None
        for fact in facts:
            actual = name_by_fact.get(int(fact["id"]), {}).get(last)
            if actual:
                target = _plan_column_fqn(fact, actual)
                break
        if target is not None:
            rewritten.append(item.model_copy(update={"column": target}))
            for table_id in source_ids:
                table_ok[table_id] = table_ok.get(table_id, True) and True
        else:
            rewritten.append(item)
            for table_id in source_ids:
                table_ok[table_id] = False
    return rewritten, {table_id for table_id, ok in table_ok.items() if ok}


_LIST_AXIS_PLANT = ("정수장", "사업장")
_LIST_AXIS_HQ = ("본부", "유역", "권역")
_LIST_AXIS_METRIC = ("변량", "측정항목")
_LIST_AXIS_TAG = ("태그", "측정점")


def list_axis_skips_tag_identity(axis: list[str]) -> bool:
    blob = SearchMixin._compact_natural_text(" ".join(axis))
    if not blob:
        return False
    if any(marker in blob for marker in _LIST_AXIS_TAG) and not any(
        marker in blob
        for marker in (*_LIST_AXIS_PLANT, *_LIST_AXIS_HQ, *_LIST_AXIS_METRIC)
    ):
        return False
    return any(
        marker in blob
        for marker in (*_LIST_AXIS_PLANT, *_LIST_AXIS_HQ, *_LIST_AXIS_METRIC)
    )


def list_axis_identity_column_names(
    columns: list[dict[str, Any]],
    table: dict[str, Any] | None,
    axis: list[str],
) -> list[str]:
    """Axis code+name only. Never invent names or keep tagsn on a plant/hq list."""

    del table
    blob_axis = SearchMixin._compact_natural_text(" ".join(axis))
    wanted: list[str] = []
    if any(marker in blob_axis for marker in _LIST_AXIS_PLANT):
        wanted.extend(("사업장", "정수장", "suj"))
    if any(marker in blob_axis for marker in _LIST_AXIS_HQ):
        wanted.extend(("본부", "유역", "bnb"))
    if any(marker in blob_axis for marker in _LIST_AXIS_METRIC):
        wanted.extend(("변량", "br"))
    names: list[str] = []
    seen: set[str] = set()
    for column in columns:
        name = str(column.get("name") or "").strip()
        if not name or name in seen or name.casefold() == "tagsn":
            continue
        ident = name.casefold()
        compact_ident = ident.replace("_", "")
        col_blob = _column_identity_blob(column)
        if not any(
            token in ident or token in compact_ident or token in col_blob
            for token in wanted
        ):
            continue
        is_code = ident.endswith(("_code", "_cd")) or "코드" in col_blob
        is_name = ident.endswith(("_name", "_nm")) or any(
            marker in col_blob for marker in ("이름", "명칭")
        )
        if not is_code and not is_name:
            continue
        seen.add(name)
        names.append(name)
    return names


def _mapping_is_measure(mapping: dict[str, Any]) -> bool:
    blob = SearchMixin._compact_natural_text(
        " ".join(
            [
                str(mapping.get("logical_name") or ""),
                str(mapping.get("column_fqn") or ""),
                str(mapping.get("column_name") or ""),
            ]
        )
    )
    if any(word in blob for word in ("사업장", "정수장", "sujcode")):
        return False
    return any(word in blob for word in ("별량", "측정항목"))


def _measure_needles(
    query: str,
    mappings: list[dict[str, Any]],
    analysis: QueryAnalysis | None = None,
) -> list[str]:
    if analysis is not None and meaning_failed(analysis):
        return []
    compact_q = SearchMixin._compact_natural_text(query)
    needles: list[str] = []
    seen: set[str] = set()
    confirmed: set[str] = set()
    for mapping in mappings:
        if not _mapping_is_measure(mapping):
            continue
        if str(mapping.get("code_value") or "").strip():
            confirmed.add(
                SearchMixin._compact_natural_text(
                    str(mapping.get("matched_mention") or "")
                )
            )
            confirmed.add(
                SearchMixin._compact_natural_text(
                    str(mapping.get("natural_value") or "")
                )
            )
    confirmed.discard("")
    for mapping in mappings:
        if not _mapping_is_measure(mapping):
            continue
        mention = str(mapping.get("matched_mention") or "")
        if is_list_target_mention(query, mention):
            continue
        for raw in (
            mention,
            mapping.get("natural_value"),
        ):
            token = SearchMixin._compact_natural_text(str(raw or ""))
            if len(token) < 2 or token not in compact_q or token in seen:
                continue
            if token in confirmed:
                continue
            if is_list_target_mention(query, token):
                continue
            seen.add(token)
            needles.append(token)
    return needles


def _tag_label_column(
    columns: list[dict[str, Any]],
    table: dict[str, Any],
) -> str | None:
    names = dimension_identity_column_names(columns, table)
    preferred: list[str] = []
    rest: list[str] = []
    by_name = {
        str(column.get("name") or "").strip(): column
        for column in columns
        if str(column.get("name") or "").strip()
    }
    for name in names:
        blob = _column_identity_blob(by_name.get(name) or {})
        if any(marker in blob for marker in ("설명", "별칭")):
            preferred.append(name)
        else:
            rest.append(name)
    ordered = [*preferred, *rest]
    return ordered[0] if ordered else None


def measure_point_label_filters(
    query: str,
    mappings: list[dict[str, Any]],
    *,
    tables_by_id: dict[int, dict[str, Any]],
    columns_by_id: dict[int, list[dict[str, Any]]],
    table_ids: set[int],
    analysis: QueryAnalysis | None = None,
) -> list[PlannedFilter]:
    """Keep measure-code rows whose tag label still contains the measure word."""

    needles = _measure_needles(query, mappings, analysis)
    if not needles:
        return []
    filters: list[PlannedFilter] = []
    for table_id in sorted(table_ids):
        table = tables_by_id.get(int(table_id))
        if not is_tag_master_table(table):
            continue
        assert table is not None
        label = _tag_label_column(columns_by_id.get(int(table_id), []), table)
        if not label:
            continue
        fqn = _plan_column_fqn(table, label)
        for needle in needles:
            filters.append(
                PlannedFilter(
                    meaning=f"측정점라벨:{needle}",
                    column=fqn,
                    operator="LIKE",
                    value=f"%{needle}%",
                    resolution_status="resolved",
                    confidence=1.0,
                )
            )
    return filters


def fact_time_column_names(columns: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for column in columns:
        if not _column_is_store_date(column) or _column_looks_like_audit_date(column):
            continue
        name = str(column.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _column_is_store_date(column: dict[str, Any]) -> bool:
    metadata = _metadata_dict(column.get("metadata"))
    pattern = (
        metadata.get("format_pattern")
        or column.get("format_pattern")
        or None
    )
    dtype = str(column.get("dtype") or column.get("data_type") or "")
    return _format_looks_like_date(pattern) or _dtype_looks_like_date(dtype)


_LIST_QUERY_TOKENS = ("목록", "리스트")
_LIST_ANSWER_CUES = ("목록", "리스트", "알려")
_LIST_COORDINATORS = ("이나", "또는", "및")
_INVENTORY_QUERY_TOKENS = ("어떤 게", "어떤게", "어떤 것", "무엇이 있", "뭐가 있")


def _mention_type_stem(token: str) -> str:
    stem = SearchMixin._compact_natural_text(token)
    for suffix in ("별", "들"):
        if stem.endswith(suffix) and len(stem) - len(suffix) >= 2:
            stem = stem[: -len(suffix)]
    return stem


def _mention_is_measure_item(analysis: QueryAnalysis | None, mention: str) -> bool:
    """측정 항목 표면은 목록의 답 축으로 보아도 코드 필터에서 빼지 않는다."""

    if analysis is None:
        return False
    key = SearchMixin._compact_natural_text(mention)
    if not key:
        return False
    metric = SearchMixin._compact_natural_text(
        measure_item_surface(
            str(analysis.metric or "")
            or str(getattr(analysis.measurement, "metric", "") or "")
        )
    )
    if metric and (key == metric or metric in key or key in metric):
        return True
    for role in analysis.schema_roles or []:
        role_name = SearchMixin._compact_natural_text(str(role.role or ""))
        if "측정" not in role_name:
            continue
        for term in role.search_terms or []:
            if SearchMixin._compact_natural_text(str(term or "")) == key:
                return True
    return False


def _mention_is_answer_axis(analysis: QueryAnalysis | None, mention: str) -> bool:
    """목록의 답 축(정수장 목록의 정수장)만 값 필터에서 뺀다. 측정 항목은 빼지 않는다."""

    if analysis is None:
        return False
    if str(analysis.procedure or "").strip() != "list":
        return False
    if _mention_is_measure_item(analysis, mention):
        return False
    outputs = list(analysis.primary_outputs or [])
    axis = [axis_mention(item) for item in outputs]
    return is_answer_axis_text(mention, outputs) or is_answer_axis_text(
        mention, axis
    )


def is_groupby_mention(query: str, token: str) -> bool:
    """X별 is a group-by stem. It is not a bind of every X code."""

    stem = _mention_type_stem(token)
    compact_q = SearchMixin._compact_natural_text(query)
    return bool(stem) and len(stem) >= 2 and f"{stem}별" in compact_q


def is_category_mention(query: str, token: str) -> bool:
    """X들 / X별 name a type, not every stored code of that type."""

    if is_groupby_mention(query, token):
        return True
    stem = _mention_type_stem(token)
    compact_q = SearchMixin._compact_natural_text(query)
    return bool(stem) and len(stem) >= 2 and f"{stem}들" in compact_q


def mention_is_dimension_type(mention: str, rows: list[dict[str, Any]]) -> bool:
    """Mention that is the dimension's own type name is a seed, not a value."""

    stem = _mention_type_stem(mention)
    if not stem or len(stem) < 2:
        return False
    for row in rows:
        logical = SearchMixin._compact_natural_text(
            str(row.get("logical_name") or "")
        )
        if logical and stem in logical:
            return True
    return False


def is_dimension_list_query(query: str) -> bool:
    text = query or ""
    return any(token in text for token in _LIST_QUERY_TOKENS) or any(
        token in text for token in _INVENTORY_QUERY_TOKENS
    )


def is_list_target_mention(query: str, token: str) -> bool:
    """X목록, or X이나 Y 알려주세요: X/Y are the answer grain, not code values.

    권역 인스턴스를 '또는'으로 이은 것은 범위 OR이지 목록 축이 아니다.
    """

    stem = SearchMixin._compact_natural_text(token)
    compact_q = SearchMixin._compact_natural_text(query)
    if not stem or len(stem) < 2:
        return False
    peeled = peel_type_suffix(token)
    if peeled is not None and peeled[0]:
        return False
    if any(f"{stem}{tail}" in compact_q for tail in _LIST_QUERY_TOKENS):
        return True
    if not any(cue in compact_q for cue in _LIST_ANSWER_CUES):
        return False
    return any(
        f"{stem}{coord}" in compact_q or f"{coord}{stem}" in compact_q
        for coord in _LIST_COORDINATORS
    )


_AGG_QUERY_TOKENS = (
    "평균",
    "합계",
    "총합",
    "건수",
    "최대",
    "최소",
    "최댓값",
    "최솟값",
)


def asks_aggregation(query: str = "", analysis: QueryAnalysis | None = None) -> bool:
    """Aggregation/extremum from procedure, measurement, or 평균/합계 in the question."""

    procedure = str(getattr(analysis, "procedure", "") or "").strip() if analysis else ""
    if procedure in {"aggregate", "extremum"}:
        return True
    if analysis is not None:
        if str(getattr(analysis.measurement, "aggregation", "") or "").strip():
            return True
        blob = SearchMixin._compact_natural_text(
            " ".join(
                [
                    str(analysis.metric or ""),
                    str(getattr(analysis.measurement, "metric", "") or ""),
                    *[str(item) for item in (analysis.primary_outputs or [])],
                ]
            )
        )
        if any(token in blob for token in _AGG_QUERY_TOKENS):
            return True
    compact_q = SearchMixin._compact_natural_text(query or "")
    return any(token in compact_q for token in _AGG_QUERY_TOKENS)


def _analysis_metric(analysis: QueryAnalysis | None) -> str:
    if analysis is None:
        return ""
    metric = str(analysis.metric or analysis.measurement.metric or "").strip()
    if metric:
        return metric
    for role in analysis.schema_roles or []:
        role_name = SearchMixin._compact_natural_text(str(role.role or ""))
        if "측정" not in role_name:
            continue
        return " ".join(str(term) for term in (role.search_terms or [])).strip()
    return ""


def is_catalog_list_query(query: str = "", analysis: QueryAnalysis | None = None) -> bool:
    """Master/inventory listing. Not a time-scoped measured reading."""

    if _asks_series(query):
        return False
    procedure = str(getattr(analysis, "procedure", "") or "").strip() if analysis else ""
    if procedure == "list":
        return True
    return is_dimension_list_query(query)


def asks_measured_reading(query: str = "", analysis: QueryAnalysis | None = None) -> bool:
    """Answer is a fact reading. procedure=lookup does not make this a catalog lookup."""

    if asks_aggregation(query, analysis) or _asks_series(query):
        return True
    if is_catalog_list_query(query, analysis):
        return False
    return bool(_analysis_metric(analysis))


def measurement_needs_period(query: str = "", analysis: QueryAnalysis | None = None) -> bool:
    """Measured reading with an empty period slot. Do not invent latest."""

    period_text = str(getattr(analysis, "period", "") or "").strip() if analysis else ""
    if parse_period_from_query(query, period_text) is not None:
        return False
    return asks_measured_reading(query, analysis)


def query_requests_fact(
    query: str = "",
    period: ParsedPeriod | None = None,
    analysis: QueryAnalysis | None = None,
    mappings: list[dict[str, Any]] | None = None,
) -> bool:
    """집계·극값·시계열, 또는 측정 항목이면 팩트가 필요하다. 목록은 제외."""

    del mappings, period
    return asks_measured_reading(query, analysis)


def is_embedding_recall_row(row: dict[str, Any]) -> bool:
    """Embedding/vector hits are recall only. They do not bind codes."""

    match_type = str(row.get("match_type") or "").strip().casefold()
    source = str(row.get("source") or "").strip().casefold()
    return match_type in {"embedding", "vector", "semantic"} or source in {
        "vector",
        "embedding",
        "semantic",
    }


def approved_code_mappings(rows: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [row for row in (rows or []) if not is_embedding_recall_row(row)]


def embedding_recall_mappings(
    rows: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    return [row for row in (rows or []) if is_embedding_recall_row(row)]


def value_mappings_for_plan(
    mappings: list[dict[str, Any]],
    query: str,
    analysis: QueryAnalysis | None = None,
) -> list[dict[str, Any]]:
    """Keep value bindings that belong in filters/entities. Type/HQ-displaced rows stay as table seeds."""

    return [
        mapping
        for mapping in mappings
        if not is_category_mention(query, str(mapping.get("matched_mention") or ""))
        and not is_list_target_mention(query, str(mapping.get("matched_mention") or ""))
        and not _mention_is_answer_axis(
            analysis, str(mapping.get("matched_mention") or "")
        )
        and not is_displaced_plant_mapping(query, mapping, mappings)
    ]


def _row_licensed_for_mention(row: dict[str, Any], query: str) -> bool:
    mention = str(row.get("matched_mention") or query)
    natural = str(row.get("natural_value") or "")
    if SearchMixin._label_matches_needle(natural, mention):
        return True
    mention_peel = peel_type_suffix(mention)
    natural_peel = peel_type_suffix(
        SearchMixin._natural_label_head(natural) or natural
    )
    if mention_peel is None or natural_peel is None:
        return False
    mention_instance, mention_group = mention_peel
    natural_instance, natural_group = natural_peel
    return bool(
        mention_instance
        and natural_instance == mention_instance
        and mention_group.name == natural_group.name
    )


def _rows_for_single_mention_codes(
    rows: list[dict[str, Any]],
    query: str,
) -> list[dict[str, Any]]:
    """같은 컬럼에 걸린 여러 include 언급은 OR이므로 코드를 합친다.

    라벨이 언급과 맞지 않는 그룹만 뺀다. 한 언급만 남기지 않는다.
    """

    if len(rows) <= 1 or not query:
        return rows
    by_mention: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = SearchMixin._compact_natural_text(str(row.get("matched_mention") or ""))
        by_mention.setdefault(key or "_", []).append(row)
    if len(by_mention) <= 1:
        return rows
    licensed: list[dict[str, Any]] = []
    for group in by_mention.values():
        if any(_row_licensed_for_mention(row, query) for row in group):
            licensed.extend(group)
    return licensed


def mapping_filters(
    mappings: list[dict[str, Any]],
    query: str = "",
    analysis: QueryAnalysis | None = None,
) -> list[PlannedFilter]:
    if analysis is not None and meaning_failed(analysis):
        return []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for mapping in mappings:
        if is_embedding_recall_row(mapping):
            continue
        mention = str(mapping.get("matched_mention") or "")
        if query and is_category_mention(query, mention):
            continue
        if query and is_list_target_mention(query, mention):
            continue
        if _mention_is_answer_axis(analysis, mention):
            continue
        if query and is_displaced_plant_mapping(query, mapping, mappings):
            continue
        fqn = str(mapping.get("column_fqn") or "").strip()
        code = str(mapping.get("code_value") or "").strip()
        if not fqn or not code:
            continue
        polarity = str(mapping.get("filter_polarity") or "include").strip().lower()
        if polarity not in {"include", "exclude"}:
            polarity = "include"
        grouped.setdefault((fqn.casefold(), polarity), []).append(mapping)
    filters: list[PlannedFilter] = []
    for rows in grouped.values():
        rows = _rows_for_single_mention_codes(rows, query)
        codes = sorted(
            {
                str(row.get("code_value") or "").strip()
                for row in rows
                if str(row.get("code_value") or "").strip()
            }
        )
        if not codes:
            continue
        mention = str(rows[0].get("matched_mention") or "")
        if query and mention_is_dimension_type(mention, rows):
            continue
        fqn = str(rows[0].get("column_fqn") or "")
        natural = str(rows[0].get("natural_value") or "")
        polarity = str(rows[0].get("filter_polarity") or "include").strip().lower()
        if polarity == "exclude":
            operator = "NOT_IN" if len(codes) > 1 else "NE"
        else:
            operator = "IN" if len(codes) > 1 else "EQ"
        filters.append(
            PlannedFilter(
                meaning=f"코드매핑:{natural}",
                column=fqn,
                operator=operator,  # type: ignore[arg-type]
                value=",".join(codes),
                resolution_status="resolved",
                confidence=1.0,
            )
        )
    return filters
