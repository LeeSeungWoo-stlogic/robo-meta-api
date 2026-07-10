"""Entity probe registry — datasource별 code table YAML 로더 (A안)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List

import yaml

from ..config import settings
from .neo4j_client.models import ColumnCandidate

_RULES_DIR = Path(__file__).parent.parent / "rules"


@dataclass(frozen=True)
class ProbeSpec:
    table: str
    entity_type: str
    code_column: str
    label_columns: tuple[str, ...]
    pg_schema: str
    neo4j_schema: str


def _registry_path(datasource: str) -> Path:
    ds = (datasource or "rwis").strip().lower()
    return _RULES_DIR / f"{ds}_code_probe.yaml"


@lru_cache(maxsize=8)
def _load_registry_raw(datasource: str) -> dict:
    path = _registry_path(datasource)
    if not path.is_file():
        raise FileNotFoundError(f"entity probe registry not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"invalid probe registry format: {path}")
    return data


def load_probe_specs(datasource: str | None = None) -> List[ProbeSpec]:
    ds = (datasource or settings.entity_probe_datasource or "rwis").strip().lower()
    raw = _load_registry_raw(ds)
    pg_schema = str(raw.get("pg_schema") or settings.source_pg_schema or "RWIS").strip()
    neo4j_schema = str(raw.get("neo4j_schema") or ds).strip()
    specs: list[ProbeSpec] = []
    for item in raw.get("probes") or []:
        if not isinstance(item, dict):
            continue
        table = str(item.get("table") or "").strip()
        code_col = str(item.get("code_column") or "").strip()
        labels = item.get("label_columns") or []
        if not table or not code_col or not labels:
            continue
        label_cols = tuple(str(c).strip() for c in labels if str(c or "").strip())
        if not label_cols:
            continue
        specs.append(
            ProbeSpec(
                table=table,
                entity_type=str(item.get("entity_type") or "code").strip() or "code",
                code_column=code_col,
                label_columns=label_cols,
                pg_schema=pg_schema,
                neo4j_schema=neo4j_schema,
            )
        )
    return specs


def probe_specs_to_columns(specs: List[ProbeSpec]) -> List[ColumnCandidate]:
    """registry → batch_db_probe용 ColumnCandidate 목록."""
    out: list[ColumnCandidate] = []
    for spec in specs:
        for label in spec.label_columns:
            out.append(
                ColumnCandidate(
                    table_schema=spec.neo4j_schema,
                    table_name=spec.table,
                    name=label,
                    dtype="",
                    description="",
                )
            )
    return out


def spec_for_column(
    specs: List[ProbeSpec], *, table_name: str, label_column: str
) -> ProbeSpec | None:
    table_key = (table_name or "").strip().upper()
    label_key = (label_column or "").strip().lower()
    for spec in specs:
        if spec.table.upper() != table_key:
            continue
        if label_key in {c.lower() for c in spec.label_columns}:
            return spec
    return None
