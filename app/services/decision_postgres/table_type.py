"""Map pipeline subject_area to the metadata-list table type."""

from __future__ import annotations

from typing import Literal

ListTableType = Literal["Raw", "Fact", "Code", "Dimension"]

_SUBJECT_AREA_TO_LIST_TYPE: dict[str, ListTableType] = {
    "raw": "Raw",
    "agg": "Fact",
    "code": "Code",
    "master": "Dimension",
}

_FACT_ROLE_TYPES = frozenset({"Fact", "Raw"})
_MASTER_ROLE_TYPES = frozenset({"Dimension", "Code"})
_TAG_MASTER_TYPES = frozenset({"Dimension"})


def list_table_type(subject_area: str | None) -> ListTableType | None:
    """Return Raw/Fact/Code/Dimension, or None when the area is out of this slice.

    `link` is a cross-system identifier bridge, not a code dictionary.
    `hist` and `unknown` are also excluded.
    """
    key = str(subject_area or "").strip().casefold()
    return _SUBJECT_AREA_TO_LIST_TYPE.get(key)


def allowed_table_types_for_role(role_text: str) -> frozenset[str] | None:
    """Which list types may fill a schema role. None = no type gate.

    Fact/measurement roles: Fact or Raw only. Code/Dimension cannot be the
    measurement table. Tag-master roles: Dimension only (Code is a dictionary,
    not the tag entity). Other master/code roles: Dimension or Code.
    Physical table names are not used.
    """
    text = str(role_text or "").casefold()
    if "마스터" in text:
        if "태그" in text:
            return _TAG_MASTER_TYPES
        return _MASTER_ROLE_TYPES
    if any(token in text for token in ("팩트", "시계열", "계측", "집계")):
        return _FACT_ROLE_TYPES
    return None


def table_type_allows_role(role_text: str, subject_area: str | None) -> bool:
    """False when the table's list type cannot fill the role.

    link/hist/unknown have no list type and cannot fill a gated role.
    """
    allowed = allowed_table_types_for_role(role_text)
    if allowed is None:
        return True
    typed = list_table_type(subject_area)
    if typed is None:
        return False
    return typed in allowed
