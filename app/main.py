"""robo-meta-api FastAPI entry.

라이프사이클:
- 시작 시: Neo4j async 드라이버 + 원천 PG 풀 init.
- 종료 시: 양쪽 모두 close.

/health 응답에는 v0.6 RC 정책에 따라 meta_version 동봉 + X-Meta-Version 헤더 노출.
"""
from __future__ import annotations

import asyncio
import sys

# K-AIR 차용 query_runner 의 psycopg async 가 Windows ProactorEventLoop 와 충돌.
# uvicorn import 전에 Selector 정책으로 전환 (K-AIR-meta-api/app/main.py 패턴 그대로).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from .db import close_metadata_repository, init_metadata_repository
from .routers import decision, decision_v2, meta, query_exec
from .runtime_config import get_runtime, init_runtime
from .schemas import META_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ = app
    init_runtime()
    await init_metadata_repository()
    yield
    await close_metadata_repository()


app = FastAPI(
    title="robo-meta-api v4",
    description=(
        "Neo4j 베이스 v0.7 meta-api — A안 entity resolution (resolved_entities). "
        "v0.6 8 endpoint path 유지."
    ),
    version=f"meta-{META_VERSION}",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _attach_meta_version_header(request, call_next):
    resp = await call_next(request)
    resp.headers["X-Meta-Version"] = META_VERSION
    return resp


@app.get("/health")
async def health(response: Response) -> dict:
    """K-AIR v0.6 RC: meta_version 동봉 + X-Meta-Version 헤더 (미들웨어가 자동 부착)."""
    runtime = get_runtime()
    return {
        "meta_version": META_VERSION,
        "status": "ok",
        "metadata_backend": runtime.metadata_backend,
        "execution_backend": runtime.execution.backend,
        "mindsdb_integration": runtime.execution.integration,
    }


app.include_router(decision.router)
app.include_router(meta.router)
app.include_router(query_exec.router)
# /v2/data_decision — app.state.v2_deps 배선 시 활성화 (미구성 시 503, v1 무영향)
app.include_router(decision_v2.router)


def main() -> None:
    runtime = init_runtime()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        
        config = uvicorn.Config(
            "app.main:app",
            host=runtime.api_host,
            port=runtime.api_port,
            reload=False,
            loop="asyncio"
        )
        server = uvicorn.Server(config)
        loop.run_until_complete(server.serve())
    else:
        uvicorn.run(
            "app.main:app",
            host=runtime.api_host,
            port=runtime.api_port,
            reload=False,
        )


if __name__ == "__main__":
    main()
