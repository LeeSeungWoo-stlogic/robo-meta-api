"""Pick one Fact/Raw from store labels. Do not invent a table."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from ...runtime_config import get_runtime
from ...schemas import QueryAnalysis
from .helpers import _resolve_subject_area, _serving_logical_name

FactChooserFn = Callable[[str, QueryAnalysis, list[dict[str, Any]]], Awaitable[dict[str, Any] | None]]


class _Unset:
    pass


_UNSET = _Unset()
_chooser_override: FactChooserFn | None | _Unset = _UNSET

FACT_CHOOSE_PROMPT = """\
당신은 스토어 팩트 표 선정기다.
질문과 기간 입도 힌트에 맞는 후보를 하나 고른다.
후보는 id, logical_name, description, analyzed_description, subject_area 만 있다.
이 목록의 id만 써라. 없는 id를 만들지 마라. 물리표 이름을 창작하지 마라.
고를 수 없으면 table_id는 null.
출력은 JSON 객체 하나뿐이다.
{"table_id": null}
"""


def set_fact_chooser(fn: FactChooserFn | None) -> None:
    global _chooser_override
    _chooser_override = fn


def reset_fact_chooser() -> None:
    global _chooser_override
    _chooser_override = _UNSET


def _candidate_payload(table: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(table["id"]),
        "logical_name": _serving_logical_name(table),
        "description": table.get("description"),
        "analyzed_description": table.get("analyzed_description"),
        "subject_area": _resolve_subject_area(table),
    }


async def choose_fact_from_store(
    query: str,
    analysis: QueryAnalysis,
    facts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if len(facts) <= 1:
        return facts[0] if facts else None
    if not isinstance(_chooser_override, _Unset):
        if _chooser_override is None:
            return None
        return await _chooser_override(query, analysis, facts)

    runtime = get_runtime()
    if not runtime.decision.hyde_enabled or not runtime.embedding.api_key:
        return None
    allowed = {int(table["id"]) for table in facts if table.get("id") is not None}
    hint = str(analysis.measurement.storage_type_hint or "").strip() or None
    client = AsyncOpenAI(
        api_key=runtime.embedding.api_key,
        base_url=(
            runtime.decision.analysis_base_url
            or runtime.embedding.base_url
            or None
        ),
    )
    try:
        response = await client.chat.completions.create(
            model=runtime.decision.hyde_model,
            messages=[
                {"role": "system", "content": FACT_CHOOSE_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "question": query,
                            "storage_type_hint": hint,
                            "candidates": [_candidate_payload(table) for table in facts],
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            max_completion_tokens=200,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        payload = json.loads(content)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("table_id")
    if raw is None or str(raw).strip().lower() in {"", "null", "none"}:
        return None
    try:
        picked = int(raw)
    except (TypeError, ValueError):
        return None
    if picked not in allowed:
        return None
    return next(table for table in facts if int(table["id"]) == picked)
