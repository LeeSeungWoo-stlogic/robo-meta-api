"""Closed alias groups. Do not invent store rows or question-specific names."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..metadata_repository._search import SearchMixin

# 유역 / 권역 / 지역본부는 같은 조직 단위다. 발전→사업장은 여기 두지 않는다.
_REGION_HQ_TERMS = (
    "지역본부",
    "유역본부",
    "권역본부",
    "유역",
    "권역",
)
_HQ_DICT_MARKERS = ("지역본부", "유역본부", "권역본부")
_PLANT_TYPE_MARKERS = ("사업장", "정수장")


def _compact(text: str) -> str:
    return SearchMixin._compact_natural_text(text)


def _mapping_table_blob(row: dict[str, Any]) -> str:
    return _compact(
        " ".join(
            [
                str(row.get("logical_name") or ""),
                str(row.get("original_name") or ""),
                str(row.get("name") or ""),
            ]
        )
    )


def mapping_is_hq(row: dict[str, Any]) -> bool:
    blob = _mapping_table_blob(row)
    if any(marker in blob for marker in _HQ_DICT_MARKERS):
        return True
    return "본부" in blob and not any(
        marker in blob for marker in _PLANT_TYPE_MARKERS
    )


def mapping_is_plant(row: dict[str, Any]) -> bool:
    blob = _mapping_table_blob(row)
    return any(marker in blob for marker in _PLANT_TYPE_MARKERS)


@dataclass(frozen=True)
class TypeGroup:
    name: str
    suffixes: tuple[str, ...]
    kind: str

    def row_in_dictionary(self, mapping: dict[str, Any]) -> bool:
        if self.kind == "hq":
            return mapping_is_hq(mapping)
        return mapping_is_plant(mapping)


TYPE_GROUPS: tuple[TypeGroup, ...] = (
    TypeGroup(
        name="hq",
        suffixes=tuple(sorted(_REGION_HQ_TERMS, key=len, reverse=True)),
        kind="hq",
    ),
    TypeGroup(
        name="plant",
        suffixes=tuple(sorted(_PLANT_TYPE_MARKERS, key=len, reverse=True)),
        kind="plant",
    ),
)


def _active_groups(
    groups: tuple[TypeGroup, ...] | None,
) -> tuple[TypeGroup, ...]:
    return groups if groups is not None else TYPE_GROUPS


def hq_suffixes(groups: tuple[TypeGroup, ...] | None = None) -> tuple[str, ...]:
    for group in _active_groups(groups):
        if group.name == "hq":
            return group.suffixes
    return _REGION_HQ_TERMS


def peel_type_suffix(
    surface: str,
    groups: tuple[TypeGroup, ...] | None = None,
) -> tuple[str, TypeGroup] | None:
    """Longest closed type suffix on a compact surface. One peel."""

    key = _compact(surface)
    if len(key) < 2:
        return None
    best: tuple[int, str, TypeGroup] | None = None
    for group in _active_groups(groups):
        for suffix in group.suffixes:
            token = _compact(suffix)
            if len(token) < 2 or not key.endswith(token):
                continue
            if best is None or len(token) > best[0]:
                best = (len(token), token, group)
    if best is None:
        return None
    _, token, group = best
    instance = key[: -len(token)]
    return instance, group


def type_product_surfaces(instance: str, group: TypeGroup) -> list[str]:
    stem = _compact(instance)
    if len(stem) < 2:
        return []
    seen: set[str] = set()
    surfaces: list[str] = []
    for suffix in group.suffixes:
        text = f"{stem}{_compact(suffix)}"
        if len(text) < 2 or text in seen:
            continue
        seen.add(text)
        surfaces.append(text)
    return surfaces


def range_surface_has_instance(
    surface: str,
    groups: tuple[TypeGroup, ...] | None = None,
) -> bool:
    peeled = peel_type_suffix(surface, groups)
    if peeled is None:
        return bool(_compact(surface))
    return bool(peeled[0])


def unique_code_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep every store row that already has a code. Multiple codes stay."""

    return [
        row
        for row in rows
        if str(row.get("code_value") or "").strip()
    ]


def annotate_matched_mention(
    rows: list[dict[str, Any]],
    mention: str,
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["matched_mention"] = mention
        annotated.append(item)
    return annotated


def expand_region_hq_aliases(
    query: str,
    tokens: list[str],
    groups: tuple[TypeGroup, ...] | None = None,
) -> list[str]:
    """If any region-HQ term appears, emit the whole group and compound stems."""

    terms = hq_suffixes(groups)
    compact_tokens = [SearchMixin._compact_natural_text(token) for token in tokens]
    surfaces = [
        SearchMixin._compact_natural_text(query),
        *compact_tokens,
    ]
    triggered = any(
        term and term in surface
        for surface in surfaces
        for term in terms
    ) or any(token == "본부" for token in compact_tokens)
    if not triggered:
        return []
    extras: list[str] = list(terms)
    seen = {SearchMixin._compact_natural_text(item) for item in extras}
    for token in compact_tokens:
        if not token:
            continue
        for src in terms:
            if src not in token:
                continue
            stem = token.replace(src, "", 1)
            for dst in terms:
                candidate = f"{stem}{dst}" if stem else dst
                compact = SearchMixin._compact_natural_text(candidate)
                if compact and compact not in seen:
                    seen.add(compact)
                    extras.append(candidate)
    return extras


def prefer_region_hq_mappings(
    query: str,
    mappings: list[dict],
) -> list[dict]:
    """권역/유역 is an HQ unit. Do not bind a plant row that only shares the stem."""

    compact_q = SearchMixin._compact_natural_text(query)
    if not any(term in compact_q for term in _REGION_HQ_TERMS):
        return mappings
    if not any(mapping_is_hq(row) for row in mappings):
        return mappings
    return [
        row
        for row in mappings
        if mapping_is_hq(row) or not mapping_is_plant(row)
    ]


def is_displaced_plant_mapping(
    query: str,
    mapping: dict,
    mappings: list[dict],
) -> bool:
    """HQ alias wins the value. The plant table may still be the list target."""

    compact_q = SearchMixin._compact_natural_text(query)
    if not any(term in compact_q for term in _REGION_HQ_TERMS):
        return False

    if not mapping_is_plant(mapping):
        return False
    return any(mapping_is_hq(row) for row in mappings)
