"""POST /t2sql — 확정 SQL + used 메타."""

from __future__ import annotations

from fastapi import APIRouter

from ..db import get_metadata_repository
from ..schemas import T2SqlRequest, T2SqlResponse
from ..services.t2sql import run_t2sql

router = APIRouter(tags=["t2sql"])


@router.post(
    "/t2sql",
    response_model=T2SqlResponse,
    summary="Text-to-SQL",
    description=(
        "자연어로 읽기 전용 SQL과 그 SQL에 쓰인 메타를 반환한다. "
        "`/data_decision` 0.7 계약은 변경하지 않는다. "
        "성공 SQL은 `/query_execute`에 그대로 넣을 수 있는 SourceName 3단 수식이다. "
        "timeout_s는 파이프라인 벽시계이며 미지정 시 runtime total_timeout_seconds."
    ),
)
async def t2sql(req: T2SqlRequest) -> T2SqlResponse:
    return await run_t2sql(get_metadata_repository(), req)
