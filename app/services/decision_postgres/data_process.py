"""RWIS data_process letter → VAL kind. Closed table, not a catalog mapping."""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from ...schemas import PlannedFilter

logger = logging.getLogger(__name__)

KIND_INSTANT = "instant"
KIND_LOGMEAN = "logmean"
KIND_GAUGE = "gauge"
KIND_DELTA = "delta"
KIND_RUNTIME = "runtime"
KIND_RATE = "rate"
KIND_DAY_TOTAL = "day_total"
KIND_VIRTUAL = "virtual"
KIND_CONTACT = "contact"

_LETTER_KIND: dict[str, str] = {
    "M": KIND_INSTANT,
    "Q": KIND_INSTANT,
    "P": KIND_LOGMEAN,
    "A": KIND_GAUGE,
    "D": KIND_DELTA,
    "S": KIND_RUNTIME,
    "F": KIND_RATE,
    "9": KIND_DAY_TOTAL,
    "C": KIND_DAY_TOTAL,
    "Z": KIND_DAY_TOTAL,
    "I": KIND_VIRTUAL,
    "B": KIND_CONTACT,
    "R": KIND_CONTACT,
}

_MART_LOGICAL = (
    "vw_tag_dim",
    "vw_measure_1min",
    "vw_measure_hour",
    "vw_measure_day",
)

FN_NO_SQL = "NO_SQL"
FN_DELTA = "DELTA"
FN_IDENTITY = "IDENTITY"
FN_USAGE = "USAGE"
_USAGE_KINDS = frozenset({KIND_GAUGE, KIND_DELTA})
_SAFE_LIKE = re.compile(r"^[가-힣A-Za-z0-9 ]+$")


def kind_for_letter(letter: str) -> str | None:
    return _LETTER_KIND.get(str(letter or "").strip().upper())


def unit_blocks_sum(letter: str, unit_desc: str) -> bool:
    unit = str(unit_desc or "").strip()
    key = str(letter or "").strip().upper()
    if key == "9" and unit == "%":
        return True
    if key == "F" and unit == "%":
        return True
    return False


def source_uses_process_rules(tables: Iterable[dict[str, Any]]) -> bool:
    for table in tables:
        logical = str(table.get("logical_name") or "").strip().casefold()
        schema = str(table.get("schema_name") or "").strip().casefold()
        if schema == "rwis_mart":
            return True
        if any(logical == name or logical.endswith(f".{name}") for name in _MART_LOGICAL):
            return True
    return False


def _filter_column_tail(column: str) -> str:
    return str(column or "").strip().rsplit(".", 1)[-1].casefold()


def _split_filter_values(value: str | None) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    parts: list[str] = []
    for raw in text.split(","):
        item = raw.strip().strip("'\"")
        if item:
            parts.append(item)
    return parts


_SCOPE_TAILS = frozenset({"tagsn", "suj_code", "suj_name", "br_code"})
_SAFE_CODE = re.compile(r"^[0-9A-Za-z]+$")
_SAFE_NAME = re.compile(r"^[가-힣A-Za-z0-9]+$")


def _norm_tagsn(value: Any) -> str | None:
    text = str(value or "").strip()
    if text.isdigit():
        return text
    try:
        number = float(text)
    except ValueError:
        return None
    if number.is_integer() and number >= 0:
        return str(int(number))
    return None


def _safe_scope_value(column: str, value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if column == "tagsn":
        return _norm_tagsn(text)
    if column in {"suj_code", "br_code"} and _SAFE_CODE.fullmatch(text):
        return text
    if column == "suj_name" and _SAFE_NAME.fullmatch(text):
        return text
    return None


def letter_scope_sql(filters: Iterable[PlannedFilter] | None) -> str | None:
    """WHERE for already-bound tag scope. Does not read the question."""

    by_column: dict[str, list[str]] = {}
    for planned in filters or []:
        if str(getattr(planned, "resolution_status", "") or "") != "resolved":
            continue
        operator = str(getattr(planned, "operator", "") or "")
        if operator not in {"EQ", "IN"}:
            continue
        tail = _filter_column_tail(str(getattr(planned, "column", "") or ""))
        if tail not in _SCOPE_TAILS:
            continue
        for raw in _split_filter_values(getattr(planned, "value", None)):
            safe = _safe_scope_value(tail, raw)
            if safe and safe not in by_column.setdefault(tail, []):
                by_column[tail].append(safe)
    if "tagsn" in by_column:
        literals = ",".join(f"'{code}'" for code in by_column["tagsn"])
        return f"tagsn IN ({literals})"
    parts: list[str] = []
    for column in ("suj_code", "suj_name", "br_code"):
        codes = by_column.get(column) or []
        if not codes:
            continue
        literals = ",".join(f"'{code}'" for code in codes)
        parts.append(f"{column} IN ({literals})")
    if not parts:
        return None
    return " AND ".join(parts)


def metric_match_sql(needles: Iterable[str]) -> str | None:
    """Item words on tag dim. Not a data_process letter filter."""

    clauses: list[str] = []
    seen: set[str] = set()

    def _like(column: str, text: str) -> str:
        return f"{column} LIKE '%{text.replace(chr(39), '')}%'"

    for needle in needles:
        text = str(needle or "").strip()
        if len(text) < 2 or not _SAFE_LIKE.fullmatch(text):
            continue
        variants = [text]
        compact = text.replace(" ", "")
        if compact.endswith("유량") and len(compact) > 2:
            variants.append(compact[:-2] + " 유량")
        for item in variants:
            if item in seen:
                continue
            seen.add(item)
            for column in ("tag_desc", "br_name", "metric_name"):
                clauses.append(_like(column, item))
        if "유량" in compact and compact != "유량":
            head = compact.replace("유량", "")
            if len(head) >= 2:
                token = f"{head}|유량"
                if token not in seen:
                    seen.add(token)
                    clauses.append(
                        "(tag_desc LIKE '%"
                        + head.replace("'", "")
                        + "%' AND tag_desc LIKE '%유량%')"
                    )
    if not clauses:
        return None
    return "(" + " OR ".join(clauses) + ")"


def metric_tag_where(
    filters: Iterable[PlannedFilter] | None,
    needles: Iterable[str],
) -> str | None:
    location = letter_scope_sql(filters)
    if not location or location.startswith("tagsn "):
        return None
    metric = metric_match_sql(needles)
    if not metric:
        return None
    return f"{location} AND {metric}"


def bound_tagsn(
    filters: Iterable[PlannedFilter] | None,
    mappings: Iterable[dict[str, Any]] | None = None,
) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def _add(code: str) -> None:
        text = str(code or "").strip()
        code = _norm_tagsn(text)
        if code and code not in seen:
            seen.add(code)
            found.append(code)

    for planned in filters or []:
        if str(getattr(planned, "resolution_status", "") or "") != "resolved":
            continue
        if _filter_column_tail(str(getattr(planned, "column", "") or "")) != "tagsn":
            continue
        for item in _split_filter_values(getattr(planned, "value", None)):
            _add(item)
    for mapping in mappings or []:
        ident = str(mapping.get("column_name") or mapping.get("column_fqn") or "")
        tail = ident.rsplit(".", 1)[-1].casefold()
        if tail != "tagsn":
            continue
        _add(str(mapping.get("code_value") or mapping.get("value") or ""))
    return found


def bound_suj_names(filters: Iterable[PlannedFilter] | None) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for planned in filters or []:
        if str(getattr(planned, "resolution_status", "") or "") != "resolved":
            continue
        if _filter_column_tail(str(getattr(planned, "column", "") or "")) != "suj_name":
            continue
        for item in _split_filter_values(getattr(planned, "value", None)):
            if item.startswith("%") or item.endswith("%"):
                continue
            if item and item not in seen:
                seen.add(item)
                found.append(item)
    return found


async def load_process_rows(
    *,
    filters: Iterable[PlannedFilter] | None,
    repository: Any = None,
    source_instance_id: str | None = None,
) -> list[dict[str, str]]:
    where = letter_scope_sql(filters)
    if not where or repository is None or not str(source_instance_id or "").strip():
        return []
    try:
        from ..execution_context_resolver import resolve_execution_context
        from ..query_runner_mindsdb import execute as mindsdb_execute
    except Exception as exc:
        logger.warning("data_process mindsdb unavailable: %s", exc)
        return []
    try:
        resolved = await resolve_execution_context(
            repository,
            source_instance_id=str(source_instance_id).strip(),
            requested_objects=["vw_tag_dim"],
        )
        catalog = str(resolved.source_name or resolved.catalog or "").strip()
        if not catalog:
            logger.warning("data_process letter read failed: serving catalog empty")
            return []
        sql = (
            f"SELECT DISTINCT tagsn, data_process, unit_desc "
            f"FROM `{catalog}`.`vw_tag_dim` WHERE {where}"
        )
        result = await mindsdb_execute(
            sql=sql,
            timeout_s=10,
            max_rows=50,
            caller="data_process",
            execution_context=resolved,
        )
    except Exception as exc:
        logger.warning("data_process letter read failed: %s", exc)
        return []
    if str(result.get("status") or "") not in {"ok", "success", ""}:
        logger.warning(
            "data_process letter read status=%s error=%s",
            result.get("status"),
            result.get("error"),
        )
        return []
    return _rows_from_tag_probe(result)


def _rows_from_tag_probe(result: dict[str, Any]) -> list[dict[str, str]]:
    columns = [str(name).casefold() for name in (result.get("columns") or [])]
    tagsn_idx = columns.index("tagsn") if "tagsn" in columns else None
    letter_idx = columns.index("data_process") if "data_process" in columns else 0
    unit_idx = columns.index("unit_desc") if "unit_desc" in columns else 1
    out: list[dict[str, str]] = []
    for row in result.get("rows") or []:
        if not isinstance(row, (list, tuple)) or not row:
            continue
        key = str(row[letter_idx] if letter_idx < len(row) else "").strip()
        if not key:
            continue
        unit = str(row[unit_idx] if unit_idx < len(row) else "")
        tagsn = ""
        if tagsn_idx is not None and tagsn_idx < len(row):
            tagsn = _norm_tagsn(row[tagsn_idx]) or ""
        out.append({"letter": key, "unit_desc": unit, "tagsn": tagsn})
    return out


async def probe_metric_tag_rows(
    *,
    filters: Iterable[PlannedFilter] | None,
    needles: Iterable[str],
    repository: Any = None,
    source_instance_id: str | None = None,
) -> list[dict[str, str]]:
    where = metric_tag_where(filters, needles)
    if not where or repository is None or not str(source_instance_id or "").strip():
        return []
    try:
        from ..execution_context_resolver import resolve_execution_context
        from ..query_runner_mindsdb import execute as mindsdb_execute
    except Exception as exc:
        logger.warning("data_process tag probe unavailable: %s", exc)
        return []
    try:
        resolved = await resolve_execution_context(
            repository,
            source_instance_id=str(source_instance_id).strip(),
            requested_objects=["vw_tag_dim"],
        )
        catalog = str(resolved.source_name or resolved.catalog or "").strip()
        if not catalog:
            return []
        sql = (
            f"SELECT DISTINCT tagsn, data_process, unit_desc "
            f"FROM `{catalog}`.`vw_tag_dim` WHERE {where}"
        )
        result = await mindsdb_execute(
            sql=sql,
            timeout_s=10,
            max_rows=50,
            caller="data_process_tag_probe",
            execution_context=resolved,
        )
    except Exception as exc:
        logger.warning("data_process tag probe failed: %s", exc)
        return []
    if str(result.get("status") or "") not in {"ok", "success", ""}:
        logger.warning(
            "data_process tag probe status=%s error=%s",
            result.get("status"),
            result.get("error"),
        )
        return []
    return _rows_from_tag_probe(result)


_PROCESS_NEEDLE = frozenset({"적산", "적산차", "순시", "순간", "평균", "합계", "사용량", "총합"})


def tag_probe_needles(needles: Iterable[str]) -> list[str]:
    """Item surfaces only. Drop process words and digits."""

    out: list[str] = []
    for needle in needles:
        text = str(needle or "").strip()
        compact = "".join(text.split())
        if len(compact) < 2 or compact.isdigit() or compact in _PROCESS_NEEDLE:
            continue
        if text not in out:
            out.append(text)
    return out


def tagsn_codes_from_rows(rows: Iterable[dict[str, str]]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for row in rows:
        code = _norm_tagsn(row.get("tagsn"))
        if code and code not in seen:
            seen.add(code)
            found.append(code)
    return found


def replace_tagsn_filter(
    filters: list[PlannedFilter],
    *,
    column: str,
    codes: list[str],
    meaning: str = "측정 항목 태그",
) -> list[PlannedFilter]:
    if not column or not codes:
        return list(filters)
    planned = PlannedFilter(
        meaning=meaning,
        column=column,
        operator="IN" if len(codes) > 1 else "EQ",
        value=",".join(codes) if len(codes) > 1 else codes[0],
        resolution_status="resolved",
        confidence=1.0,
    )
    out: list[PlannedFilter] = []
    replaced = False
    for item in filters:
        if (
            str(getattr(item, "resolution_status", "") or "") == "resolved"
            and _filter_column_tail(str(getattr(item, "column", "") or "")) == "tagsn"
        ):
            if not replaced:
                out.append(planned)
                replaced = True
            continue
        out.append(item)
    if not replaced:
        out.append(planned)
    return out


def asks_usage(query: str, asked: str) -> bool:
    if str(asked or "").strip().upper() == "SUM":
        return True
    return any(token in str(query or "") for token in ("사용량", "합계", "총합"))


def usage_keep_rows(rows: list[dict[str, str]], *, query: str, asked: str) -> list[dict[str, str]]:
    if not asks_usage(query, asked):
        return rows
    kept = [
        row
        for row in rows
        if kind_for_letter(str(row.get("letter") or "")) in _USAGE_KINDS
    ]
    return kept


def refine_function(
    *,
    asked: str,
    procedure: str,
    rows: list[dict[str, str]],
    grain: str | None,
    query: str = "",
) -> tuple[str, str | None]:
    """Return (function, tag_combine). asked is SUM/AVG/MAX/MIN/IDENTITY."""

    function = str(asked or "").strip().upper()
    usage = asks_usage(query, function)
    letters = [str(row.get("letter") or "").strip().upper() for row in rows if row.get("letter")]
    kinds = {kind_for_letter(letter) for letter in letters}
    kinds.discard(None)
    if not kinds:
        return function, None
    grain_key = str(grain or "").strip().lower()
    if (function == "SUM" or usage) and len(kinds) > 1:
        if usage and kinds <= _USAGE_KINDS:
            parts: list[str] = []
            for row in rows:
                letter = str(row.get("letter") or "").strip().upper()
                kind = kind_for_letter(letter)
                tagsn = str(row.get("tagsn") or "").strip()
                if kind == KIND_GAUGE:
                    parts.append(f"{tagsn}:A:DELTA")
                elif kind == KIND_DELTA:
                    parts.append(f"{tagsn}:D:IDENTITY")
            return FN_USAGE, ";".join(parts)
        return FN_NO_SQL, "글자가 섞여 한 합계를 만들지 않음"
    if any(unit_blocks_sum(row.get("letter") or "", row.get("unit_desc") or "") for row in rows):
        if function == "SUM" or usage:
            return FN_NO_SQL, "글자와 단위가 어긋나 합계를 만들지 않음"
    kind = next(iter(kinds))
    if function == "SUM" or (usage and kind == KIND_GAUGE):
        if kind in {KIND_INSTANT, KIND_LOGMEAN, KIND_VIRTUAL, KIND_CONTACT}:
            return FN_NO_SQL, "이 처리 종류는 합계를 만들지 않음"
        if kind == KIND_GAUGE:
            return FN_DELTA, "period_end_minus_prev"
        if kind == KIND_RATE and grain_key == "day":
            return FN_IDENTITY, "day_total_as_stored"
        if kind == KIND_DAY_TOTAL and grain_key == "hour":
            return FN_NO_SQL, "당일 누적 시간값을 더하지 않음"
        if kind == KIND_DAY_TOTAL and grain_key == "day":
            return "SUM", None
    if function == "AVG" and kind == KIND_LOGMEAN:
        return FN_IDENTITY, "stored_logmean"
    if function == "AVG" and kind == KIND_GAUGE:
        return FN_NO_SQL, "누적 눈금은 평균하지 않음"
    if procedure == "lookup":
        return FN_IDENTITY, None
    return function, None
