from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from ...schemas import FilterRequirement, PlannedFilter, QueryAnalysis
from ..metadata_repository import PostgresMetadataRepository
from ..metadata_repository._search import SearchMixin
from .default_date import _dtype_looks_like_date, _format_looks_like_date
from .period import extract_year, parse_korean_period


_HANGUL = re.compile(r"[가-힣]")
_CODE_LITERAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$")
_NAME_MARKERS = ("NAME", "NM", "DESC", "TITLE", "LABEL", "ALIAS", "COMMENT")
_CODE_MARKERS = ("CODE", "CD", "TAGSN", "_ID", "ID_")
_DATE_NAME_MARKERS = ("TIME", "DATE", "DT", "TM")
_AUDIT_DATE_MARKERS = (
    "CRDT",
    "CRT_DT",
    "CREATED",
    "CREATE_DT",
    "CRE_DT",
    "UPDT",
    "UPD_DT",
    "UPDATED",
    "UPDATE_DT",
    "REG_DT",
    "REGDT",
    "MOD_DT",
    "MODDT",
    "INST_DT",
    "INSTDT",
)
_MEASURE_MEANING_TOKENS = (
    "평균",
    "농도",
    "측정값",
    "미만",
    "이상",
    "최고",
    "최저",
    "ph",
)
_PERIOD_MEANING_TOKENS = (
    "기간",
    "날짜",
    "일자",
    "연도",
    "연월",
    "year",
    "month",
    "월",
    "시각",
    "시점",
)
_NUMERIC_DTYPES = (
    "int",
    "numeric",
    "decimal",
    "float",
    "double",
    "real",
    "number",
    "serial",
)


def _compact_text(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def _has_natural_script(value: str | None) -> bool:
    return bool(_HANGUL.search(value or ""))


def _is_code_literal(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text or _has_natural_script(text):
        return False
    return bool(_CODE_LITERAL.fullmatch(text))


def _column_dtype(column: dict[str, Any] | None) -> str:
    if not column:
        return ""
    return str(column.get("dtype") or column.get("data_type") or "").lower()


def _dtype_is_numeric(column: dict[str, Any] | None) -> bool:
    dtype = _column_dtype(column)
    return any(token in dtype for token in _NUMERIC_DTYPES)


def _column_accepts_natural_value(column: dict[str, Any] | None) -> bool:
    """Name/text columns may keep a surface mention. Code/numeric columns may not."""

    if not column:
        return False
    name = str(column.get("name") or "").upper()
    if _dtype_is_numeric(column):
        return False
    if any(marker in name for marker in _CODE_MARKERS):
        return False
    return any(marker in name for marker in _NAME_MARKERS)


def _column_is_date_like(column: dict[str, Any] | None) -> bool:
    if not column:
        return False
    metadata = column.get("metadata")
    pattern = metadata.get("format_pattern") if isinstance(metadata, dict) else None
    return _format_looks_like_date(pattern) or _dtype_looks_like_date(
        _column_dtype(column)
    )


def _column_looks_like_audit_date(column: dict[str, Any] | None) -> bool:
    """Create/update stamp columns. Period bind prefers measure time over these."""

    if not column:
        return False
    name = str(column.get("name") or "").upper().replace("-", "_")
    return any(marker in name for marker in _AUDIT_DATE_MARKERS)


def _meaning_looks_like_measure(meaning: str | None) -> bool:
    text = _compact_text(meaning)
    return any(token in text for token in _MEASURE_MEANING_TOKENS)


def _column_format_pattern(column: dict[str, Any]) -> str:
    metadata = column.get("metadata")
    if isinstance(metadata, str):
        try:
            parsed = json.loads(metadata)
            metadata = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            metadata = {}
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("format_pattern") or "").casefold().replace("-", "").replace(" ", "")


def _varchar_period_between(
    column: dict[str, Any],
    period,
) -> tuple[str, str] | None:
    """Finer compact clocks than the period window use a closed range, not LIKE."""

    pattern = _column_format_pattern(column)
    if not pattern:
        return None
    start = period.start_date()
    last = period.end_date_exclusive() - timedelta(days=1)
    day_start = start.strftime("%Y%m%d")
    day_last = last.strftime("%Y%m%d")
    if any(token in pattern for token in ("hh", "hour")):
        return "BETWEEN", f"{day_start}00,{day_last}23"
    if "dd" in pattern:
        return "BETWEEN", f"{day_start},{day_last}"
    return None


def _period_bind_value(column: dict[str, Any], period) -> tuple[str, str]:
    """Return (operator, value) for a parsed period on a date-like column."""

    if getattr(period, "week_start", None) is not None:
        return _week_bind_value(column, period)
    if _dtype_looks_like_date(_column_dtype(column)):
        start = period.start_date().isoformat()
        end = period.end_date_exclusive().isoformat()
        return "BETWEEN", f"{start},{end}"
    compact = _varchar_period_between(column, period)
    if compact is not None:
        return compact
    return "LIKE", f"{period.like_prefix}%"


def _week_bind_value(column: dict[str, Any], period) -> tuple[str, str]:
    start = period.week_start
    end = period.week_end
    if start is None or end is None:
        return "LIKE", f"{period.like_prefix}%"
    if _dtype_looks_like_date(_column_dtype(column)):
        return "BETWEEN", f"{start.isoformat()},{end.isoformat()}"
    metadata = column.get("metadata")
    if isinstance(metadata, str):
        try:
            parsed = json.loads(metadata)
            metadata = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    pattern = str(metadata.get("format_pattern") or "").casefold().replace("-", "")
    compact_start = start.strftime("%Y%m%d")
    compact_end = end.strftime("%Y%m%d")
    if any(token in pattern for token in ("hh", "hour", "yyyy mmddhh", "yyyymmddhh")):
        return "BETWEEN", f"{compact_start}00,{compact_end}23"
    if "yyyy" in pattern or "yy" in pattern or not pattern:
        return "BETWEEN", f"{compact_start},{compact_end}"
    return "BETWEEN", f"{compact_start},{compact_end}"


def _meaning_looks_like_period(meaning: str | None) -> bool:
    text = str(meaning or "").casefold()
    return any(token in text for token in _PERIOD_MEANING_TOKENS)


def _period_from_requirement(
    requirement: FilterRequirement,
    requirements: list[FilterRequirement],
):
    """Bind a calendar phrase only. Do not inherit a sibling year onto 유역/사업장."""

    parsed = parse_korean_period(requirement.value_text)
    if parsed is not None:
        return parsed
    if not _meaning_looks_like_period(requirement.meaning):
        return None
    fallback_year = None
    for other in requirements:
        if other is requirement:
            continue
        year = extract_year(other.value_text) or extract_year(other.meaning)
        if year is not None:
            fallback_year = year
            break
    return parse_korean_period(requirement.value_text, fallback_year=fallback_year)


def _is_numeric_literal(value: str | None) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


def _column_looks_like_code(column: dict[str, Any] | None) -> bool:
    if not column:
        return False
    name = str(column.get("name") or "").upper()
    return any(marker in name for marker in _CODE_MARKERS)


def _column_looks_like_measure_value(column: dict[str, Any] | None) -> bool:
    if not column:
        return False
    tokens = set(re.findall(r"[A-Z]+", str(column.get("name") or "").upper()))
    return bool(tokens & {"VAL", "VALUE", "AMT", "QTY"})


def _surface_value_may_resolve(
    value: str | None,
    column: dict[str, Any] | None,
    meaning: str | None = None,
) -> bool:
    """Bind a non-mapping value only when the literal type matches the column kind.

    Hangul and alphabetic codes need a Store mapping. A name-column hit is not
    a resolved code. Numeric literals may bind to numeric/value columns. A
    numeric threshold does not bind to a code column.
    """

    text = str(value or "").strip()
    if not text or column is None:
        return False
    if _has_natural_script(text):
        return False
    if _is_numeric_literal(text):
        if _column_looks_like_code(column) and _meaning_looks_like_measure(meaning):
            return False
        return (
            _column_looks_like_code(column)
            or _dtype_is_numeric(column)
            or _column_looks_like_measure_value(column)
        )
    if _is_code_literal(text):
        return False
    return False


def _forward_label_in_value(natural: str, target: str) -> bool:
    """Store label ⊂ slot text, but not a Hangul prefix of a longer name.

    '탁도' ⊂ '탁도', '충청지역' ⊂ '충청지역'. Reject '청주' ⊂ '청주정수장'.
    """
    if not natural or not target:
        return False
    if natural == target:
        return True
    index = target.find(natural)
    if index < 0:
        return False
    before = target[index - 1] if index else ""
    after = target[index + len(natural) :]
    if before and SearchMixin._is_hangul_char(before):
        return False
    if after and SearchMixin._is_hangul_char(after[0]):
        return False
    return True


def _mapping_matches_value_text(mapping: dict[str, Any], value_text: str | None) -> bool:
    if not value_text:
        return False
    natural = _compact_text(str(mapping.get("natural_value") or ""))
    target = _compact_text(value_text)
    if natural and _forward_label_in_value(natural, target):
        return True
    if natural and target and SearchMixin._token_is_label_mention(target, natural):
        return True
    for surface in mapping.get("matched_surfaces") or []:
        if _compact_text(str(surface)) == target:
            return True
    mention = _compact_text(str(mapping.get("matched_mention") or ""))
    if mention and mention == target:
        return True
    code = _compact_text(str(mapping.get("code_value") or ""))
    return bool(code) and code == target


def _mapping_cluster(
    mappings: list[dict[str, Any]],
    requirement: FilterRequirement,
) -> list[dict[str, Any]]:
    """All verified mappings that share the same mention and column as the slot."""

    primary = next(
        (
            mapping
            for mapping in mappings
            if _mapping_matches_value_text(mapping, requirement.value_text)
        ),
        None,
    )
    if primary is None:
        return []
    mention = str(primary.get("matched_mention") or "").strip()
    column_fqn = str(primary.get("column_fqn") or "")
    clustered: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for mapping in mappings:
        if str(mapping.get("column_fqn") or "") != column_fqn:
            continue
        same_slot = _mapping_matches_value_text(mapping, requirement.value_text)
        same_mention = bool(mention) and str(mapping.get("matched_mention") or "") == mention
        if not same_slot and not same_mention:
            continue
        code = str(mapping.get("code_value") or "").strip()
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        clustered.append(mapping)
    return clustered


def _plan_column_from_fqn(column_fqn: str) -> str:
    parts = column_fqn.split(".")
    # Store seed uses db.schema.table.column; plan uses schema.table.column.
    if len(parts) >= 3:
        return ".".join(parts[-3:])
    return column_fqn


def _plan_column_table_in_scope(
    column_fqn: str,
    *,
    table_ids: list[int],
    tables_by_id: dict[int, dict[str, Any]],
) -> bool:
    """Mapping FQN is resolved only when that table is already in the plan set."""

    parts = [part for part in column_fqn.split(".") if part]
    if len(parts) < 2:
        return False
    table = parts[-2].casefold()
    schema = parts[-3].casefold() if len(parts) >= 3 else ""
    allowed: set[tuple[str, str]] = set()
    for table_id in table_ids:
        row = tables_by_id.get(int(table_id))
        if not row:
            continue
        allowed.add(
            (
                str(row.get("schema_name") or "").casefold(),
                str(row.get("original_name") or row.get("name") or "").casefold(),
            )
        )
    if (schema, table) in allowed:
        return True
    if schema:
        return False
    return any(name == table for _, name in allowed)


def _with_metric_filter_requirements(
    analysis: QueryAnalysis,
    mappings: list[dict[str, Any]] | None = None,
) -> list[FilterRequirement]:
    """Promote measurement.metric into a filter slot when Store VM can resolve it.

    Generic: no table/column names. Uses analyzer metric text + verified mappings.
    """
    requirements = list(analysis.filter_requirements)
    metric = str(analysis.measurement.metric or "").strip()
    if not metric:
        return requirements
    if mappings is not None and not any(
        _mapping_matches_value_text(mapping, metric) for mapping in mappings
    ):
        return requirements
    compact_metric = _compact_text(metric)
    for requirement in requirements:
        if requirement.value_text and _compact_text(requirement.value_text) == compact_metric:
            return requirements
        if compact_metric and compact_metric in _compact_text(requirement.meaning):
            return requirements
    requirements.append(
        FilterRequirement(
            meaning=f"측정항목:{metric}",
            required=True,
            operator_hint="EQ",
            value_text=metric,
        )
    )
    return requirements


def _with_mapping_filter_requirements(
    analysis: QueryAnalysis,
    mappings: list[dict[str, Any]] | None = None,
) -> list[FilterRequirement]:
    """Promote verified Store mappings mentioned in the question into filter slots.

    Generic: no table/column names. Analyzer slots are not required.
    """
    requirements = list(analysis.filter_requirements)
    promoted_fqns: set[str] = set()
    for mapping in mappings or []:
        natural = str(mapping.get("natural_value") or "").strip()
        code = str(mapping.get("code_value") or "").strip()
        column_fqn = str(mapping.get("column_fqn") or "").strip()
        if not natural or not code or not column_fqn:
            continue
        if any(
            _mapping_matches_value_text(mapping, requirement.value_text)
            or (
                requirement.value_text
                and _compact_text(requirement.value_text) == _compact_text(natural)
            )
            for requirement in requirements
        ):
            continue
        fqn_key = column_fqn.casefold()
        if fqn_key in promoted_fqns:
            continue
        promoted_fqns.add(fqn_key)
        requirements.append(
            FilterRequirement(
                meaning=f"코드매핑:{natural}",
                required=True,
                operator_hint="EQ",
                value_text=natural,
            )
        )
    return requirements


def _filter_column_score(
    requirement: FilterRequirement,
    column: dict[str, Any],
) -> float:
    base = float(column.get("score") or 0.0)
    meaning = requirement.meaning.lower()
    name = str(column.get("name") or "").upper()
    descriptions = " ".join(
        str(column.get(key) or "").lower()
        for key in ("description", "analyzed_description")
    )
    boosts = (
        (("오류", "에러"), ("ERR", "ERROR")),
        (("상태",), ("STATUS", "STATE")),
        (("시간", "시각", "기간"), ("TIME", "DATE", "DT", "TM")),
        (("이름", "명칭"), ("NAME", "NM", "DESC")),
        (("코드",), ("CODE", "CD")),
        (("값", "측정"), ("VALUE", "VAL")),
    )
    lexical = 0.0
    for korean_terms, physical_terms in boosts:
        if any(term in meaning for term in korean_terms) and any(
            term in name for term in physical_terms
        ):
            lexical = max(lexical, 0.45)
    tokens = {
        token
        for token in re.findall(r"[가-힣]{2,}", meaning)
        if token not in {"있는", "항목", "조회"}
    }
    if tokens and any(token in descriptions for token in tokens):
        lexical = max(lexical, 0.25)
    return base + lexical


async def _resolve_filters(
    repository: PostgresMetadataRepository,
    *,
    requirements: list[FilterRequirement],
    embeddings: dict[str, list[float]],
    table_ids: list[int],
    tables_by_id: dict[int, dict[str, Any]],
    mappings: list[dict[str, Any]],
    minimum_similarity: float,
) -> tuple[list[PlannedFilter], list[str]]:
    planned: list[PlannedFilter] = []
    unresolved: list[str] = []
    for index, requirement in enumerate(requirements):
        embedding = embeddings.get(f"filter:{index}")
        best: dict[str, Any] | None = None
        options: list[dict[str, Any]] = []
        if embedding is not None and table_ids:
            grouped = await repository.search_columns(
                embedding,
                table_ids=table_ids,
                per_table_limit=5,
            )
            options = [
                item
                for values in grouped.values()
                for item in values
            ]
            if options:
                best = max(
                    options,
                    key=lambda item: _filter_column_score(
                        requirement,
                        item,
                    ),
                )

        period = _period_from_requirement(requirement, requirements)
        if period is not None:
            date_options = [item for item in options if _column_is_date_like(item)]
            measure_dates = [
                item
                for item in date_options
                if not _column_looks_like_audit_date(item)
            ]
            date_pool = measure_dates or date_options
            date_column = None
            if date_pool:
                date_column = max(
                    date_pool,
                    key=lambda item: _filter_column_score(requirement, item),
                )
            elif _column_is_date_like(best) and not _column_looks_like_audit_date(best):
                date_column = best
            elif _column_is_date_like(best) and not date_options:
                date_column = best
            if date_column is not None:
                table = (
                    tables_by_id.get(int(date_column["table_id"]))
                    if date_column.get("table_id") is not None
                    else None
                )
                if table is not None:
                    operator, value = _period_bind_value(date_column, period)
                    column = (
                        f"{table.get('schema_name')}."
                        f"{table.get('original_name') or table.get('name')}."
                        f"{date_column.get('name')}"
                    )
                    planned.append(
                        PlannedFilter(
                            meaning=requirement.meaning,
                            column=column,
                            operator=operator,
                            value=value,
                            resolution_status="resolved",
                            confidence=1.0,
                        )
                    )
                    continue

        mapped_hits = [
            mapping
            for mapping in _mapping_cluster(mappings, requirement)
            if mapping.get("column_fqn")
            and _plan_column_table_in_scope(
                str(mapping["column_fqn"]),
                table_ids=table_ids,
                tables_by_id=tables_by_id,
            )
        ]
        # Prefer verified code mapping column over weak semantic column hits.
        if mapped_hits:
            mapped_fqn = str(mapped_hits[0]["column_fqn"])
            column = _plan_column_from_fqn(mapped_fqn)
            codes = [
                str(mapping.get("code_value") or "").strip()
                for mapping in mapped_hits
                if str(mapping.get("code_value") or "").strip()
            ]
            planned.append(
                PlannedFilter(
                    meaning=requirement.meaning,
                    column=column,
                    operator="IN" if len(codes) > 1 else "EQ",
                    value=",".join(codes) if len(codes) > 1 else codes[0],
                    resolution_status="resolved",
                    confidence=1.0,
                )
            )
            continue

        score = min(1.0, (
            _filter_column_score(requirement, best)
            if best
            else 0.0
        ))
        is_resolved = bool(best and score >= minimum_similarity)
        if (
            is_resolved
            and requirement.value_text
            and not _surface_value_may_resolve(
                requirement.value_text,
                best,
                requirement.meaning,
            )
        ):
            is_resolved = False
        table = (
            tables_by_id.get(int(best["table_id"]))
            if best and best.get("table_id") is not None and is_resolved
            else None
        )
        column = (
            f"{table.get('schema_name')}.{table.get('original_name') or table.get('name')}."
            f"{best.get('name')}"
            if table and best
            else None
        )
        value = requirement.value_text
        planned.append(
            PlannedFilter(
                meaning=requirement.meaning,
                column=column or None,
                operator=requirement.operator_hint,
                value=value,
                resolution_status="resolved" if is_resolved else "unresolved",
                confidence=score,
            )
        )
        if requirement.required and not is_resolved:
            unresolved.append(f"필수 필터 컬럼 미해결: {requirement.meaning}")
    return planned, unresolved


def _filter_endpoint_key(schema: str, table: str, column: str) -> tuple[str, str, str]:
    return (
        str(schema or "").casefold(),
        str(table or "").casefold(),
        str(column or "").casefold(),
    )


def _parse_plan_column(column: str | None) -> tuple[str, str, str] | None:
    if not column:
        return None
    parts = column.split(".")
    if len(parts) < 3:
        return None
    return _filter_endpoint_key(parts[-3], parts[-2], parts[-1])


def _propagate_filters_along_fk(
    planned: list[PlannedFilter],
    edge_rows: list[dict[str, Any]],
    *,
    anchor_table_ids: set[int],
    tables_by_id: dict[int, dict[str, Any]],
) -> list[PlannedFilter]:
    """Copy resolved EQ/IN filters across one approved FK hop onto plan/bridge tables.

    Uses only Store `t2s_fk_constraints` edges. Does not invent table/column names.
    """
    if not planned or not edge_rows or not anchor_table_ids:
        return planned

    existing = {
        (filter_.column, filter_.operator, filter_.value)
        for filter_ in planned
        if filter_.column
    }
    # Undirected adjacency: endpoint -> [(other_table_id, other_schema, other_table, other_col)]
    adjacency: dict[tuple[str, str, str], list[tuple[int, str, str, str]]] = {}
    for edge in edge_rows:
        from_key = _filter_endpoint_key(
            str(edge.get("from_schema") or ""),
            str(edge.get("from_table") or ""),
            str(edge.get("from_column") or ""),
        )
        to_key = _filter_endpoint_key(
            str(edge.get("to_schema") or ""),
            str(edge.get("to_table") or ""),
            str(edge.get("to_column") or ""),
        )
        adjacency.setdefault(from_key, []).append(
            (
                int(edge["to_table_id"]),
                str(edge.get("to_schema") or ""),
                str(edge.get("to_table") or ""),
                str(edge.get("to_column") or ""),
            )
        )
        adjacency.setdefault(to_key, []).append(
            (
                int(edge["from_table_id"]),
                str(edge.get("from_schema") or ""),
                str(edge.get("from_table") or ""),
                str(edge.get("from_column") or ""),
            )
        )

    extra: list[PlannedFilter] = []
    for filter_ in planned:
        if filter_.resolution_status != "resolved":
            continue
        if filter_.operator not in {"EQ", "IN", "NE", "NOT_IN"} or filter_.value is None or not filter_.column:
            continue
        endpoint = _parse_plan_column(filter_.column)
        if endpoint is None:
            continue
        for table_id, schema, table, column in adjacency.get(endpoint, []):
            if table_id not in anchor_table_ids:
                continue
            # Prefer published table identity from tables_by_id when present.
            table_row = tables_by_id.get(table_id)
            if table_row is not None:
                schema = str(table_row.get("schema_name") or schema)
                table = str(
                    table_row.get("original_name") or table_row.get("name") or table
                )
            target_column = f"{schema}.{table}.{column}"
            key = (target_column, filter_.operator, filter_.value)
            if key in existing:
                continue
            existing.add(key)
            extra.append(
                PlannedFilter(
                    meaning=f"{filter_.meaning}→FK",
                    column=target_column,
                    operator=filter_.operator,
                    value=filter_.value,
                    resolution_status="resolved",
                    confidence=min(1.0, float(filter_.confidence or 0.0) * 0.95),
                )
            )
    if not extra:
        return planned
    return [*planned, *extra]
