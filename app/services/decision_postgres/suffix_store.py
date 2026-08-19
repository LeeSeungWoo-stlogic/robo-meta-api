"""Serving type-suffix dictionary. Store rows replace a group only when complete."""

from __future__ import annotations

from typing import Any

from .aliases import TYPE_GROUPS, TypeGroup
from ..metadata_repository._search import SearchMixin

_HQ_REQUIRED = "권역"
_PLANT_REQUIRED = "정수장"
_KNOWN_GROUPS = frozenset({"hq", "plant"})


def _compact(text: str) -> str:
    return SearchMixin._compact_natural_text(text)


def _required_suffix(group_name: str) -> str:
    if group_name == "hq":
        return _HQ_REQUIRED
    return _PLANT_REQUIRED


def _group_complete(group_name: str, suffixes: list[str]) -> bool:
    required = _compact(_required_suffix(group_name))
    if not required:
        return False
    return any(_compact(item) == required for item in suffixes)


def merge_type_groups(
    rows: list[Any] | None,
    constants: tuple[TypeGroup, ...] = TYPE_GROUPS,
) -> tuple[TypeGroup, ...]:
    """Replace one closed group only when that group's store rows are complete.

    Empty rows keep TYPE_GROUPS. An HQ group without 권역 keeps the constant HQ
    group. Plant rows do not delete HQ constants.
    """

    by_name: dict[str, list[str]] = {}
    kinds: dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("group_name") or row.get("name") or "").strip()
        suffix = str(row.get("suffix") or "").strip()
        kind = str(row.get("kind") or name).strip() or name
        if name not in _KNOWN_GROUPS or len(_compact(suffix)) < 2:
            continue
        bucket = by_name.setdefault(name, [])
        if suffix not in bucket:
            bucket.append(suffix)
        kinds[name] = kind if kind in _KNOWN_GROUPS else name
    merged: list[TypeGroup] = []
    for group in constants:
        store_suffixes = by_name.get(group.name) or []
        if not store_suffixes or not _group_complete(group.name, store_suffixes):
            merged.append(group)
            continue
        merged.append(
            TypeGroup(
                name=group.name,
                suffixes=tuple(
                    sorted(store_suffixes, key=lambda item: len(_compact(item)), reverse=True)
                ),
                kind=kinds.get(group.name, group.kind),
            )
        )
    return tuple(merged)


async def load_type_groups(repository: Any) -> tuple[TypeGroup, ...]:
    finder = getattr(repository, "find_type_suffix_groups", None)
    rows: list[Any] = []
    if callable(finder):
        try:
            fetched = await finder()
        except Exception:
            fetched = []
        if isinstance(fetched, list):
            rows = fetched
    return merge_type_groups(rows)
