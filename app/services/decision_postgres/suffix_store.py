"""Serving type-suffix dictionary. Store rows replace a group only when complete."""

from __future__ import annotations

from typing import Any

from .aliases import TYPE_GROUPS, TypeGroup
from ..metadata_repository._search import SearchMixin

_KNOWN_GROUPS = frozenset({"hq", "plant"})


def _compact(text: str) -> str:
    return SearchMixin._compact_natural_text(text)


def _compact_set(values: list[str] | tuple[str, ...]) -> set[str]:
    return {_compact(item) for item in values if _compact(item)}


def _group_complete(group: TypeGroup, suffixes: list[str]) -> bool:
    need = _compact_set(group.suffixes)
    have = _compact_set(suffixes)
    return bool(need) and need <= have


def _markers_complete(group: TypeGroup, markers: list[str]) -> bool:
    need = _compact_set(group.markers)
    have = _compact_set(markers)
    return bool(need) and need <= have


def _row_markers(row: dict[str, Any]) -> list[str]:
    raw = row.get("dictionary_markers")
    if not isinstance(raw, list):
        return []
    markers: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in markers:
            markers.append(text)
    return markers


def merge_type_groups(
    rows: list[Any] | None,
    constants: tuple[TypeGroup, ...] = TYPE_GROUPS,
) -> tuple[TypeGroup, ...]:
    """Replace one closed group only when that group's store suffixes are complete.

    Empty or partial rows keep TYPE_GROUPS. Completeness is the full constant
    suffix set, not a single required token. Plant rows do not delete HQ
    constants. Markers follow the same gate.
    """

    by_name: dict[str, list[str]] = {}
    markers_by_name: dict[str, list[str]] = {}
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
        marker_bucket = markers_by_name.setdefault(name, [])
        for marker in _row_markers(row):
            if marker not in marker_bucket:
                marker_bucket.append(marker)
    merged: list[TypeGroup] = []
    for group in constants:
        store_suffixes = by_name.get(group.name) or []
        if not store_suffixes or not _group_complete(group, store_suffixes):
            merged.append(group)
            continue
        store_markers = markers_by_name.get(group.name) or []
        markers = (
            tuple(
                sorted(
                    store_markers,
                    key=lambda item: len(_compact(item)),
                    reverse=True,
                )
            )
            if _markers_complete(group, store_markers)
            else group.markers
        )
        merged.append(
            TypeGroup(
                name=group.name,
                suffixes=tuple(
                    sorted(
                        store_suffixes,
                        key=lambda item: len(_compact(item)),
                        reverse=True,
                    )
                ),
                kind=kinds.get(group.name, group.kind),
                markers=markers,
            )
        )
    return tuple(merged)


async def load_type_groups(repository: Any) -> tuple[TypeGroup, ...]:
    finder = getattr(repository, "find_synonym_groups", None)
    rows: list[Any] = []
    if callable(finder):
        try:
            fetched = await finder(kind="type_suffix")
        except Exception:
            fetched = []
        if isinstance(fetched, list):
            rows = fetched
    return merge_type_groups(rows)


async def load_term_synonym_groups(
    repository: Any,
    needles: list[str] | None,
) -> list[dict[str, Any]]:
    finder = getattr(repository, "find_synonym_groups", None)
    if not callable(finder):
        return []
    try:
        fetched = await finder(needles, kind="term")
    except Exception:
        return []
    return fetched if isinstance(fetched, list) else []
