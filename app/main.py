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

from .config import settings
from .db import close_neo4j, close_source_pool, init_neo4j, init_source_pool
from .routers import decision, meta, query_exec
from .schemas import META_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_neo4j()
    await init_source_pool()
    yield
    await close_source_pool()
    await close_neo4j()


app = FastAPI(
    title="robo-meta-api",
    description=(
        "Neo4j 베이스 v0.6 RC body 정합 meta-api. "
        "자산 ①(test_K_Water/neo4j_client) 의 검색 파이프라인을 v0.6 RC 응답 계약으로 정렬한 gen-2."
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
    return {
        "meta_version": META_VERSION,
        "status": "ok",
        "neo4j_uri": settings.neo4j_uri,
        "source_pg": f"{settings.source_pg_host}:{settings.source_pg_port}/{settings.source_pg_db}",
        "openai_enabled": settings.openai_enabled,
    }


app.include_router(decision.router)
app.include_router(meta.router)
app.include_router(query_exec.router)


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        
        config = uvicorn.Config(
            "app.main:app",
            host=settings.api_host,
            port=settings.api_port,
            reload=False,
            loop="asyncio"
        )
        server = uvicorn.Server(config)
        loop.run_until_complete(server.serve())
    else:
        uvicorn.run(
            "app.main:app",
            host=settings.api_host,
            port=settings.api_port,
            reload=False,
        )


if __name__ == "__main__":
    main()
