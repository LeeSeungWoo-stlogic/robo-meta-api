"""`/query_execute` 라우터 — 외부 AI가 보낸 SQL을 원천 DB에 실행 대행."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..db import get_metadata_repository
from ..schemas import (
    QueryExecuteRequest,
    QueryExecuteResponse,
)
from ..services.execution_context_resolver import resolve_execution_context
from ..services import query_runner_mindsdb as query_runner
from ..services.sql_guard import GuardError
from ..services.sql_source_qualify import assert_single_sql_source
from ..services.t2sql.fingerprint import sql_fingerprint


router = APIRouter(tags=["query"])

QUERY_EXECUTE_PATH_GONE = {
    "code": "QUERY_EXECUTE_PATH_GONE",
    "message": (
        "POST /query/execute 경로는 /query_execute 로 이전되었습니다. "
        "새 경로를 사용하세요."
    ),
}


async def _resolve_artifact_payload(request: Request, artifact_id: str) -> dict:
    """Semantic View artifact_id 검증면 — ADR-002 Wave 2 / §2.1 fail-closed."""
    _ = request, artifact_id
    raise HTTPException(
        status_code=410,
        detail={
            "code": "SEMANTIC_ARTIFACT_GONE",
            "message": (
                "artifact_id 검증면은 Serving MVP에서 폐기되었습니다 "
                "(ADR-002 Wave 2 / §2.1). artifact_id 없이 /query_execute를 호출하세요."
            ),
        },
    )


@router.post(
    "/query_execute",
    response_model=QueryExecuteResponse,
    summary="Query Execute",
    description=(
        "검증된 읽기 전용 SQL을 원천 DB에 실행한다. "
        "`artifact_id`는 ADR-002 Wave 2에서 폐기 — 지정 시 **410** "
        "(`SEMANTIC_ARTIFACT_GONE`)."
    ),
    responses={
        410: {
            "description": (
                "artifact_id 검증면 폐기 (ADR-002 Wave 2 / §2.1). "
                "code=SEMANTIC_ARTIFACT_GONE"
            ),
        },
    },
)
async def query_execute(req: QueryExecuteRequest, request: Request) -> QueryExecuteResponse:
    caller = request.client.host if request.client else None
    artifact_payload = None
    if req.artifact_id:
        artifact_payload = await _resolve_artifact_payload(request, req.artifact_id)
    if req.fingerprint:
        actual = sql_fingerprint(req.sql)
        if actual != str(req.fingerprint).strip():
            raise HTTPException(
                status_code=400,
                detail="sql fingerprint does not match",
            )
    try:
        # Pipeline: claim dual-accept → SQL 단일 소스 → resolve → execute(qualify)
        sql_source_key = assert_single_sql_source(req.sql, parser_dialect="mysql")
        claimed_context = (
            req.execution_context.model_dump()
            if req.execution_context is not None
            else None
        )
        source_instance_id = None
        source_name = None
        if claimed_context is not None:
            source_instance_id = str(
                claimed_context.get("source_instance_id") or ""
            ).strip() or None
            source_name = str(claimed_context.get("source_name") or "").strip() or None
        resolved_context = await resolve_execution_context(
            get_metadata_repository(),
            claimed_context=claimed_context,
            source_instance_id=source_instance_id,
            source_name=source_name,
            sql_source_key=sql_source_key,
            allow_sql_source_resolve=True,
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


@router.post("/query/execute", include_in_schema=False)
async def query_execute_legacy_path() -> None:
    """구 경로 호환: OpenAPI에 노출하지 않고 410으로 안내."""
    raise HTTPException(status_code=410, detail=QUERY_EXECUTE_PATH_GONE)
