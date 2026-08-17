"""Store-first seeds, fact pick, and empty-meta response."""
from __future__ import annotations

import re
from typing import Any

from ...schemas import (
    DecisionResponse,
    PlannedFilter,
    QueryAnalysis,
    QueryPlan,
)
from ..decision_planner import CompositeJoinEdge, shortest_path
from ..metadata_repository._search import SearchMixin
from .default_date import _dtype_looks_like_date, _format_looks_like_date
from .filters import _column_looks_like_audit_date, _period_bind_value
from .grain import _asks_series, fallback_grains, resolve_time_grain
from .helpers import _metadata_dict, _resolve_subject_area, _serving_logical_name
from .aliases import is_displaced_plant_mapping
from .period import ParsedPeriod, parse_korean_period, week_mention
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
_FACT_UNRESOLVED = "팩트 표 미선정"
_FACT_GRAIN_UNRESOLVED = "팩트 입도를 스토어 설명과 맞출 수 없음"

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


def fact_unresolved_response(
    analysis: QueryAnalysis | None = None,
    reason: str | None = None,
) -> DecisionResponse:
    return DecisionResponse(
        target="none",
        confidence=0.0,
        candidates=[],
        threshold_used={"retrieval_axes": ["store"]},
        resolution_status="complete",
        query_analysis=analysis,
        query_plan=QueryPlan(
            completeness="partial",
            unresolved_requirements=[reason or _FACT_UNRESOLVED],
        ),
    )


def empty_meta_response(analysis: QueryAnalysis | None = None) -> DecisionResponse:
    return DecisionResponse(
        target="none",
        confidence=0.0,
        candidates=[],
        threshold_used={"retrieval_axes": ["store"]},
        resolution_status="failed",
        query_analysis=analysis,
        query_plan=QueryPlan(
            completeness="failed",
            unresolved_requirements=[_NO_META],
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
    labels = list(analysis.entities_include)
    for requirement in analysis.filter_requirements:
        if requirement.value_text:
            labels.append(requirement.value_text)
    metric = str(analysis.measurement.metric or "").strip()
    if metric:
        labels.append(metric)
    return labels


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
) -> list[dict[str, Any]]:
    """Keep facts with the most approved paths to mapping seeds."""

    if not mapped_ids or not facts:
        return list(facts)
    scored: list[tuple[int, dict[str, Any]]] = []
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
            scored.append((hits, fact))
    if not scored:
        return []
    best = max(item[0] for item in scored)
    return [fact for hits, fact in scored if hits == best]


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


def group_dimension_needles(
    query: str,
    tokens: list[str],
    extras: list[str],
) -> list[str]:
    """Group/list/HQ stems only. Prefix leftovers are not group dimensions."""

    needles: list[str] = []
    for token in tokens:
        if (
            is_groupby_mention(query, token)
            or is_category_mention(query, token)
            or is_list_target_mention(query, token)
            or SearchMixin._compact_natural_text(token) == "본부"
        ):
            needles.append(token)
    needles.extend(extras)
    return needles


def catalog_group_dimensions(
    catalog: list[dict[str, Any]],
    tokens: list[str],
) -> list[dict[str, Any]]:
    """Keep mention-matched Dimension tables as group-by seeds. No invented names."""

    needles = [
        SearchMixin._compact_natural_text(token)
        for token in tokens
        if len(SearchMixin._compact_natural_text(token)) >= 2
    ]
    kept: list[dict[str, Any]] = []
    seen: set[int] = set()
    for table in catalog:
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
        if any(needle and needle in blob for needle in needles):
            seen.add(int(table_id))
            kept.append(table)
    return kept


_LOCATION_QUERY_TOKENS = ("어디", "곳이")
_LOCATION_TABLE_MARKERS = ("사업장", "정수장", "측정위치")


def location_group_tables(
    tables: list[dict[str, Any]],
    query: str,
) -> list[dict[str, Any]]:
    """어디/곳이 asks for a place. Keep store location masters, do not invent names."""

    if not any(token in (query or "") for token in _LOCATION_QUERY_TOKENS):
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
) -> tuple[list[list[CompositeJoinEdge]], set[int], list[str]]:
    """Reach each required table from measurement/mapping anchors.

    Parent dimensions already on the path are not search origins. A place
    master must attach through the fact/tag identity, not a region parent.
    """

    selected = {int(table_id) for table_id in selected_ids}
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
        )
        if path is None:
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
    return paths, connected, unresolved


def resolve_time_role(query: str, period: ParsedPeriod | None) -> str:
    if is_dimension_list_query(query):
        return "none"
    if any(token in (query or "") for token in _EXTREMUM_TOKENS):
        return "extremum"
    if period is not None or week_mention(query):
        return "none"
    return "latest"


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
) -> PlannedFilter | None:
    period = parse_korean_period(query)
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


def measure_column_names(columns: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for column in columns:
        if _column_is_store_date(column) or _column_looks_like_audit_date(column):
            continue
        dtype = str(column.get("dtype") or column.get("data_type") or "").casefold()
        if not any(
            token in dtype
            for token in ("int", "numeric", "decimal", "float", "double", "real", "number")
        ):
            continue
        name = str(column.get("name") or "").strip()
        if name:
            names.append(name)
    return names


_LABEL_MARKERS = ("명칭", "이름")
_TAG_LABEL_MARKERS = ("명칭", "이름", "설명", "별칭")
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
) -> set[int]:
    """Series answers need the tag catalog in SELECT, not as a join-only hop."""

    kept = {int(table_id) for table_id in bridge_ids}
    if not _asks_series(query):
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


def _measure_needles(query: str, mappings: list[dict[str, Any]]) -> list[str]:
    compact_q = SearchMixin._compact_natural_text(query)
    needles: list[str] = []
    seen: set[str] = set()
    for mapping in mappings:
        if not _mapping_is_measure(mapping):
            continue
        for raw in (
            mapping.get("matched_mention"),
            mapping.get("natural_value"),
        ):
            token = SearchMixin._compact_natural_text(str(raw or ""))
            if len(token) < 2 or token not in compact_q or token in seen:
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
) -> list[PlannedFilter]:
    """Keep measure-code rows whose tag label still contains the measure word."""

    needles = _measure_needles(query, mappings)
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
_INVENTORY_QUERY_TOKENS = ("어떤 게", "어떤게", "어떤 것", "무엇이 있", "뭐가 있")
_MEASURE_BLOBS = ("별량", "측정항목", "태그")


def _mention_type_stem(token: str) -> str:
    stem = SearchMixin._compact_natural_text(token)
    for suffix in ("별", "들"):
        if stem.endswith(suffix) and len(stem) - len(suffix) >= 2:
            stem = stem[: -len(suffix)]
    return stem


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
    stem = SearchMixin._compact_natural_text(token)
    compact_q = SearchMixin._compact_natural_text(query)
    return bool(stem) and any(
        f"{stem}{tail}" in compact_q for tail in _LIST_QUERY_TOKENS
    )


def query_requests_fact(
    query: str,
    period: ParsedPeriod | None,
    analysis: QueryAnalysis | None = None,
    mappings: list[dict[str, Any]] | None = None,
) -> bool:
    """A list of dimensions does not need a fact pick."""

    if is_dimension_list_query(query):
        return False
    if period is not None or week_mention(query):
        return True
    if resolve_time_grain(query):
        return True
    if any(token in (query or "") for token in _EXTREMUM_TOKENS):
        return True
    metric = ""
    if analysis is not None:
        metric = str(analysis.measurement.metric or "").strip()
    if metric:
        return True
    for mapping in mappings or []:
        blob = SearchMixin._compact_natural_text(
            " ".join(
                [
                    str(mapping.get("logical_name") or ""),
                    str(mapping.get("column_fqn") or ""),
                ]
            )
        )
        if any(word in blob for word in _MEASURE_BLOBS):
            return True
    return False


def value_mappings_for_plan(
    mappings: list[dict[str, Any]],
    query: str,
) -> list[dict[str, Any]]:
    """Keep value bindings that belong in filters/entities. Type/HQ-displaced rows stay as table seeds."""

    return [
        mapping
        for mapping in mappings
        if not is_category_mention(query, str(mapping.get("matched_mention") or ""))
        and not is_list_target_mention(query, str(mapping.get("matched_mention") or ""))
        and not is_displaced_plant_mapping(query, mapping, mappings)
    ]


def mapping_filters(
    mappings: list[dict[str, Any]],
    query: str = "",
) -> list[PlannedFilter]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for mapping in mappings:
        mention = str(mapping.get("matched_mention") or "")
        if query and is_category_mention(query, mention):
            continue
        if query and is_list_target_mention(query, mention):
            continue
        if query and is_displaced_plant_mapping(query, mapping, mappings):
            continue
        fqn = str(mapping.get("column_fqn") or "").strip()
        code = str(mapping.get("code_value") or "").strip()
        if not fqn or not code:
            continue
        grouped.setdefault(fqn.casefold(), []).append(mapping)
    filters: list[PlannedFilter] = []
    for rows in grouped.values():
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
