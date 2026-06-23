"""/query/execute 라우터 — 외부 AI가 보낸 SQL을 원천 DB에 실행 대행."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..schemas import (
    QueryExecuteRequest,
    QueryExecuteResponse,
    QueryRequest,
    QueryResponse,
)
from ..services import query_runner
from ..services.sql_guard import GuardError


router = APIRouter(tags=["query"])


@router.post("/query", response_model=QueryResponse)
async def query_stub(req: QueryRequest) -> QueryResponse:
    """v0.7 정책: /query는 스텁 노출(미구현 상태 명시)."""
    _ = req
    return QueryResponse()


@router.post("/query/execute", response_model=QueryExecuteResponse)
async def query_execute(req: QueryExecuteRequest, request: Request) -> QueryExecuteResponse:
    caller = request.client.host if request.client else None
    try:
        result = await query_runner.execute(
            sql=req.sql,
            timeout_s=req.timeout_s,
            max_rows=req.max_rows,
            caller=caller,
        )
    except GuardError as exc:
        # API 단에서 사전 차단 — "조회 이외에는 사용 불가" 계열
        raise HTTPException(status_code=400, detail=str(exc))

    return QueryExecuteResponse(**result)
