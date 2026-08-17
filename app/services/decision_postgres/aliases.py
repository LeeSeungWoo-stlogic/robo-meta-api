"""Closed alias groups. Do not invent store rows or question-specific names."""

from __future__ import annotations

from ..metadata_repository._search import SearchMixin

# 유역 / 권역 / 지역본부는 같은 조직 단위다. 발전→사업장은 여기 두지 않는다.
_REGION_HQ_TERMS = (
    "지역본부",
    "유역본부",
    "권역본부",
    "유역",
    "권역",
)
def expand_region_hq_aliases(query: str, tokens: list[str]) -> list[str]:
    """If any region-HQ term appears, emit the whole group and compound stems."""

    compact_tokens = [SearchMixin._compact_natural_text(token) for token in tokens]
    surfaces = [
        SearchMixin._compact_natural_text(query),
        *compact_tokens,
    ]
    triggered = any(
        term and term in surface
        for surface in surfaces
        for term in _REGION_HQ_TERMS
    ) or any(token == "본부" for token in compact_tokens)
    if not triggered:
        return []
    extras: list[str] = list(_REGION_HQ_TERMS)
    seen = {SearchMixin._compact_natural_text(item) for item in extras}
    for token in compact_tokens:
        if not token:
            continue
        for src in _REGION_HQ_TERMS:
            if src not in token:
                continue
            stem = token.replace(src, "", 1)
            for dst in _REGION_HQ_TERMS:
                candidate = f"{stem}{dst}" if stem else dst
                compact = SearchMixin._compact_natural_text(candidate)
                if compact and compact not in seen:
                    seen.add(compact)
                    extras.append(candidate)
    return extras


_PLANT_TYPE_MARKERS = ("사업장", "정수장")


def prefer_region_hq_mappings(
    query: str,
    mappings: list[dict],
) -> list[dict]:
    """권역/유역 is an HQ unit. Do not bind a plant row that only shares the stem."""

    compact_q = SearchMixin._compact_natural_text(query)
    if not any(term in compact_q for term in _REGION_HQ_TERMS):
        return mappings

    def table_text(row: dict) -> str:
        return SearchMixin._compact_natural_text(
            " ".join(
                [
                    str(row.get("logical_name") or ""),
                    str(row.get("original_name") or ""),
                    str(row.get("name") or ""),
                ]
            )
        )

    def is_hq(row: dict) -> bool:
        blob = table_text(row)
        return any(
            marker in blob
            for marker in ("지역본부", "유역본부", "권역본부")
        ) or ("본부" in blob and not any(marker in blob for marker in _PLANT_TYPE_MARKERS))

    def is_plant(row: dict) -> bool:
        blob = table_text(row)
        return any(marker in blob for marker in _PLANT_TYPE_MARKERS)

    if not any(is_hq(row) for row in mappings):
        return mappings
    return [row for row in mappings if is_hq(row) or not is_plant(row)]


def is_displaced_plant_mapping(
    query: str,
    mapping: dict,
    mappings: list[dict],
) -> bool:
    """HQ alias wins the value. The plant table may still be the list target."""

    compact_q = SearchMixin._compact_natural_text(query)
    if not any(term in compact_q for term in _REGION_HQ_TERMS):
        return False

    def table_text(row: dict) -> str:
        return SearchMixin._compact_natural_text(
            " ".join(
                [
                    str(row.get("logical_name") or ""),
                    str(row.get("original_name") or ""),
                    str(row.get("name") or ""),
                ]
            )
        )

    blob = table_text(mapping)
    if not any(marker in blob for marker in _PLANT_TYPE_MARKERS):
        return False
    return any(
        any(
            marker in table_text(row)
            for marker in ("지역본부", "유역본부", "권역본부")
        )
        or (
            "본부" in table_text(row)
            and not any(marker in table_text(row) for marker in _PLANT_TYPE_MARKERS)
        )
        for row in mappings
    )
