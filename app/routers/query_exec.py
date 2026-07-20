"""/query/execute 라우터 — 외부 AI가 보낸 SQL을 원천 DB에 실행 대행."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..db import get_metadata_repository
from ..schemas import (
    QueryExecuteRequest,
    QueryExecuteResponse,
    QueryRequest,
    QueryResponse,
)
from ..services.execution_context_resolver import resolve_execution_context
from ..services import query_runner_mindsdb as query_runner
from ..services.sql_guard import GuardError


router = APIRouter(tags=["query"])


@router.post("/query", response_model=QueryResponse)
async def query_stub(req: QueryRequest) -> QueryResponse:
    """v0.7 정책: /query는 스텁 노출(미구현 상태 명시)."""
    _ = req
    return QueryResponse()


async def _resolve_artifact_payload(request: Request, artifact_id: str) -> dict:
    """Semantic View 경로: published Artifact payload를 조회한다 (fail-closed)."""
    deps = getattr(request.app.state, "v2_deps", None)
    if deps is None:
        raise HTTPException(
            status_code=503,
            detail="artifact_id 검증에 필요한 v2 배선이 구성되지 않았습니다.")
    # tenant는 이 경로에서도 검증된 token에서만 온다
    from .decision_v2 import _get_auth
    auth = _get_auth(request, deps)
    for record in await deps.store.list_published_artifacts(auth.tenant_id):
        if record.get("artifact_id") == artifact_id:
            return record["payload"]
    raise HTTPException(status_code=404,
                        detail=f"published Artifact 없음: {artifact_id}")


@router.post("/query/execute", response_model=QueryExecuteResponse)
@router.post("/query_execute", response_model=QueryExecuteResponse)
@router.post("/v1/query_execute", response_model=QueryExecuteResponse)
async def query_execute(req: QueryExecuteRequest, request: Request) -> QueryExecuteResponse:
    caller = request.client.host if request.client else None
    artifact_payload = None
    if req.artifact_id:
        artifact_payload = await _resolve_artifact_payload(request, req.artifact_id)
    try:
        claimed_context = (
            req.execution_context.model_dump()
            if req.execution_context is not None
            else None
        )
        resolved_context = await resolve_execution_context(
            get_metadata_repository(),
            claimed_context=claimed_context,
            allow_default=claimed_context is None,
        )
        result = await query_runner.execute(
            sql=req.sql,
            timeout_s=req.timeout_s,
            max_rows=req.max_rows,
            caller=caller,
            artifact_payload=artifact_payload,
            execution_context=resolved_context,
        )
    except GuardError as exc:
        # API 단에서 사전 차단 — "조회 이외에는 사용 불가" 계열
        raise HTTPException(status_code=400, detail=str(exc))

    return QueryExecuteResponse(**result)
