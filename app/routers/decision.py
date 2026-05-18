"""/data_decision 라우터 — v0.6 RC body."""
from __future__ import annotations

from fastapi import APIRouter

from ..db import get_neo4j
from ..schemas import DecisionRequest, DecisionResponse
from ..services import decision_service

router = APIRouter(tags=["decision"])


@router.post("/data_decision", response_model=DecisionResponse)
async def data_decision(req: DecisionRequest) -> DecisionResponse:
    driver = get_neo4j()
    return await decision_service.decide(
        driver,
        query=req.query,
        query_embedding=req.query_embedding,
        include_matched_columns=req.include_matched_columns,
        column_top_m=req.column_top_m,
    )
