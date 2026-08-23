"""/data_decision 라우터 — v0.6 RC body."""
from __future__ import annotations

from fastapi import APIRouter

from ..db import get_metadata_repository
from ..schemas import DecisionRequest, DecisionResponse
from ..services import decision_postgres

router = APIRouter(tags=["decision"])


@router.post(
    "/data_decision",
    response_model=DecisionResponse,
    response_model_exclude={"glossary_routes": True},
)
async def data_decision(req: DecisionRequest) -> DecisionResponse:
    repository = get_metadata_repository()
    return await decision_postgres.decide(
        repository,
        query=req.query,
        include_matched_columns=req.include_matched_columns,
        column_top_m=req.column_top_m,
        table_limit=req.table_limit,
        auto_resolve_entities=req.auto_resolve_entities,
    )
