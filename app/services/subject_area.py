"""subject_area 분류 엔진 — YAML 규칙 + AGE 온톨로지 힌트."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from ..schemas import SubjectArea

_RULES_FILE = Path(__file__).parent.parent / "rules" / "subject_area_rules.yaml"


@lru_cache(maxsize=1)
def _load_rules() -> dict:
    with _RULES_FILE.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _compile_patterns(rules: dict) -> List[tuple[re.Pattern, str]]:
    return [
        (re.compile(r["pattern"], re.IGNORECASE), r["value"])
        for r in (rules.get("name_patterns") or [])
    ]


def classify(
    schema_name: str,
    table_name: str,
    *,
    uses_by_query_count: int = 0,
    ontology_linked: bool = False,
) -> SubjectArea:
    rules = _load_rules()
    fqn = f"{schema_name}.{table_name}"
    overrides: Dict[str, str] = rules.get("overrides") or {}

    # 1) explicit override
    if fqn in overrides:
        return overrides[fqn]  # type: ignore[return-value]

    # 2) name pattern
    for pattern, value in _compile_patterns(rules):
        if pattern.search(fqn) or pattern.search(table_name):
            return value  # type: ignore[return-value]

    # 3) ontology hints
    hints = rules.get("ontology_hints") or {}
    if (
        hints.get("promote_agg_if_uses_by_query")
        and uses_by_query_count >= int(hints.get("uses_by_query_threshold", 3))
    ):
        return "agg"
    if hints.get("promote_master_if_ontology_linked") and ontology_linked:
        return "master"

    return rules.get("default") or "unknown"  # type: ignore[return-value]


def resolve_batch(
    tables: List[dict],
    uses_by_query: Optional[Dict[str, int]] = None,
    ontology_linked: Optional[set[str]] = None,
) -> Dict[str, SubjectArea]:
    """여러 테이블에 대해 분류 결과를 매핑으로 반환 (key = 'schema.table')."""
    uses = uses_by_query or {}
    linked = ontology_linked or set()
    out: Dict[str, SubjectArea] = {}
    for t in tables:
        fqn = f"{t['schema_name']}.{t['table_name']}"
        out[fqn] = classify(
            t["schema_name"],
            t["table_name"],
            uses_by_query_count=uses.get(fqn, 0),
            ontology_linked=fqn in linked,
        )
    return out
