"""LLM crosses the question with recalled Store rows. It does not invent objects."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from ...runtime_config import get_runtime
from ...schemas import QueryAnalysis

SelectFn = Callable[[str, dict[str, Any], float], Awaitable[dict[str, Any]]]
_select_override: SelectFn | None = None

SELECT_STORE_PROMPT = """\
당신은 자연어 질의와 메타 저장소 후보를 교차하는 선택기다.
store_recall에 있는 표·값매핑·조인만 써라. 없는 표·코드·컬럼을 창작하지 마라.
질문이 요구하는 범위(시설 등), 측정 항목, 기간, 집계를 모두 충족하는 메타를 고른다.
측정 항목이 답의 축이어도 버려서는 안 된다. 저장소에 측정 항목 코드나 측정점 카탈로그가 있으면 반드시 넣는다.
측정값을 묻는데 질문에 기간이 없으면 accept=false 이고 missing에 기간을 넣는다. 목록이면 기간을 missing에 넣지 마라.
목록 질의는 팩트 표를 넣지 마라.
출력은 JSON 객체 하나뿐이다.
{"accept": true, "missing": [], "selected_table_ids": [], "selected_mapping_keys": [], "reason": ""}
selected_mapping_keys 항목은 store_recall.mappings[].key 값만.
selected_table_ids는 store_recall.tables[].id 값만.
"""


def set_store_selector(fn: SelectFn | None) -> None:
    global _select_override
    _select_override = fn


def reset_store_selector() -> None:
    global _select_override
    _select_override = None


def mapping_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("table_id") or ""),
            str(row.get("column_fqn") or ""),
            str(row.get("code_value") or ""),
        ]
    )


def compact_store_recall(
    *,
    tables: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    join_edges: list[dict[str, Any]],
) -> dict[str, Any]:
    seen_tables: set[int] = set()
    table_rows: list[dict[str, Any]] = []
    for table in tables:
        table_id = table.get("id")
        if table_id is None:
            continue
        ident = int(table_id)
        if ident in seen_tables:
            continue
        seen_tables.add(ident)
        table_rows.append(
            {
                "id": ident,
                "logical_name": table.get("logical_name") or table.get("name"),
                "table_name": table.get("original_name") or table.get("name"),
                "table_type": table.get("subject_area") or table.get("table_type"),
                "description": (table.get("analyzed_description") or table.get("description") or "")[
                    :240
                ],
            }
        )
    mapping_rows = []
    for row in mappings:
        mapping_rows.append(
            {
                "key": mapping_key(row),
                "mention": row.get("matched_mention") or row.get("natural_value"),
                "natural_value": row.get("natural_value"),
                "code_value": row.get("code_value"),
                "table_id": row.get("table_id"),
                "logical_name": row.get("logical_name"),
                "column": row.get("column_name") or row.get("column_fqn"),
            }
        )
    joins = []
    for edge in join_edges[:80]:
        joins.append(
            {
                "from_table": edge.get("from_table"),
                "to_table": edge.get("to_table"),
                "from_column": edge.get("from_column"),
                "to_column": edge.get("to_column"),
            }
        )
    return {"tables": table_rows, "mappings": mapping_rows, "joins": joins}


def _llm_configured() -> bool:
    runtime = get_runtime()
    settings = getattr(runtime, "t2sql", None)
    fn = getattr(settings, "configured", None)
    if callable(fn):
        try:
            return fn() is True
        except Exception:
            return False
    return False


async def select_from_store(
    question: str,
    analysis: QueryAnalysis | None,
    recall: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any] | None:
    """Return LLM selection, or None to keep the recalled Store rows as-is."""

    if _select_override is not None:
        return await _select_override(question, recall, timeout_s)
    if not _llm_configured():
        return None
    settings = get_runtime().t2sql
    runtime = get_runtime()
    client = AsyncOpenAI(
        api_key=runtime.embedding.api_key or "test",
        base_url=settings.base_url,
        timeout=timeout_s,
        max_retries=0,
    )
    payload = {
        "question": question,
        "procedure": getattr(analysis, "procedure", None) if analysis else None,
        "metric": getattr(analysis, "metric", None) if analysis else None,
        "target": getattr(analysis, "target", None) if analysis else None,
        "period": getattr(analysis, "period", None) if analysis else None,
        "store_recall": recall,
    }
    response = await client.chat.completions.create(
        model=str(settings.model),
        messages=[
            {"role": "system", "content": SELECT_STORE_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        max_completion_tokens=800,
        response_format={"type": "json_object"},
        timeout=timeout_s,
    )
    parsed = json.loads(response.choices[0].message.content or "{}")
    if not isinstance(parsed, dict):
        return None
    return parsed


def apply_store_selection(
    selection: dict[str, Any] | None,
    *,
    mappings: list[dict[str, Any]],
    selected_ids: set[int],
    rewritten_mapping_ids: set[int] | None = None,
) -> tuple[list[dict[str, Any]], set[int]]:
    """Intersect LLM picks with Store recall. Empty pick keeps recall."""

    if not selection:
        return mappings, selected_ids
    raw_ids = selection.get("selected_table_ids") or []
    raw_keys = selection.get("selected_mapping_keys") or []
    allowed_ids = {int(item) for item in raw_ids if str(item).strip().isdigit()}
    allowed_keys = {str(item) for item in raw_keys if str(item).strip()}
    if allowed_keys:
        kept_mappings = [row for row in mappings if mapping_key(row) in allowed_keys]
        if kept_mappings:
            mappings = kept_mappings
    if allowed_ids:
        overlap = selected_ids & allowed_ids
        if overlap:
            skipped = rewritten_mapping_ids or set()
            selected_ids = overlap | {
                int(row["table_id"])
                for row in mappings
                if row.get("table_id") is not None
                and int(row["table_id"]) not in skipped
            }
    return mappings, selected_ids
