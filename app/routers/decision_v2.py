"""`/semantic_decision` — ADR-002 Wave 2: 제품 폐기 (410 Gone).

OpenAPI(`decision-v2` 태그)에서는 제외한다. 런타임 호출 시에는 410을 유지한다.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(tags=["decision-v2"], include_in_schema=False)

SEMANTIC_DECISION_GONE = {
    "code": "SEMANTIC_DECISION_GONE",
    "message": (
        "/semantic_decision은 Serving MVP에서 폐기되었습니다 "
        "(ADR-002 Wave 2). /data_decision을 사용하세요."
    ),
}


class DecisionV2Request(BaseModel):
    query: str = Field(..., min_length=1)


@router.post("/semantic_decision")
async def semantic_decision(request: Request, body: DecisionV2Request) -> dict[str, Any]:
    _ = request, body
    raise HTTPException(status_code=410, detail=SEMANTIC_DECISION_GONE)
