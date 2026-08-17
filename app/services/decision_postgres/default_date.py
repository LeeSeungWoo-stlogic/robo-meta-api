"""Pick a default date column from already-published dtype/format_pattern."""

from __future__ import annotations

from typing import Any


_DATE_DTYPES = frozenset(
    {
        "date",
        "time",
        "timestamp",
        "timestamptz",
        "timestampltz",
        "datetime",
        "timestamp without time zone",
        "timestamp with time zone",
    }
)


def _format_looks_like_date(pattern: str | None) -> bool:
    text = str(pattern or "").casefold()
    if not text:
        return False
    has_year = "yyyy" in text or "yy" in text
    has_month = "mm" in text
    return has_year and has_month


def _dtype_looks_like_date(dtype: str | None) -> bool:
    return str(dtype or "").strip().casefold() in _DATE_DTYPES


def default_date_column(columns: list[dict[str, Any]]) -> str | None:
    """Return PK date column, else the first date-like column, else None.

    Date-like means published format_pattern (year+month) or a date/time dtype.
    Does not invent column names or calendar/grain roles.
    """
    dated: list[dict[str, Any]] = []
    for column in columns:
        metadata = column.get("metadata")
        pattern = None
        if isinstance(metadata, dict):
            pattern = metadata.get("format_pattern")
        if not _format_looks_like_date(pattern) and not _dtype_looks_like_date(
            str(column.get("dtype") or "")
        ):
            continue
        dated.append(column)
    if not dated:
        return None
    for column in dated:
        if column.get("is_primary_key") or column.get("pk_ordinal") is not None:
            name = str(column.get("name") or "").strip()
            if name:
                return name
    name = str(dated[0].get("name") or "").strip()
    return name or None
