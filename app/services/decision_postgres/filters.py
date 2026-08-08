from __future__ import annotations

import re
from typing import Any

from ...schemas import FilterRequirement, PlannedFilter
from ..metadata_repository import PostgresMetadataRepository


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

        mapped = next(
            (
                mapping
                for mapping in mappings
                if requirement.value_text
                and str(mapping.get("natural_value") or "").lower()
                in requirement.value_text.lower()
            ),
            None,
        )
        # Prefer verified code mapping column over weak semantic column hits.
        if mapped is not None and mapped.get("column_fqn"):
            fqn = str(mapped["column_fqn"])
            parts = fqn.split(".")
            # Store seed uses db.schema.table.column; plan uses schema.table.column.
            column = (
                ".".join(parts[-3:])
                if len(parts) >= 3
                else fqn
            )
            value = str(mapped.get("code_value") or requirement.value_text)
            planned.append(
                PlannedFilter(
                    meaning=requirement.meaning,
                    column=column,
                    operator="EQ",
                    value=value,
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
        table = (
            tables_by_id.get(int(best["table_id"]))
            if best and best.get("table_id") is not None
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
