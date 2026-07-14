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

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from .db import close_metadata_repository, init_metadata_repository
from .routers import decision, decision_v2, meta, query_exec
from .routers.decision_v2 import V2Deps
from .runtime_config import get_runtime, init_runtime
from .schemas import META_VERSION
from .security.auth_context import AuthConfig
from .services.embedding_provider import (
    FixtureEmbeddingProvider,
    HttpEmbeddingProvider,
)
from .services.v2_store import PostgresV2Store


def _build_v2_deps(pool) -> V2Deps:
    """env 기반 `/v2/data_decision` 배선. 호출 전 runtime init·env 존재 확인 필수."""
    # 인증은 V2_JWKS_FILE·V2_ISSUER·V2_AUDIENCE가 모두 있을 때만 활성화.
    # 폐쇄망(소비처=내부 T2SQL 한정, 2026-07-13 결정)에서는 미설정 → 인증 없음.
    auth_config = None
    if all(os.environ.get(key) for key in ("V2_JWKS_FILE", "V2_ISSUER", "V2_AUDIENCE")):
        auth_config = AuthConfig.from_jwks_file(
            os.environ["V2_JWKS_FILE"],
            issuer=os.environ["V2_ISSUER"],
            audience=os.environ["V2_AUDIENCE"],
        )
    provider_kind = os.environ.get("V2_EMBEDDING_PROVIDER", "http").strip().lower()
    if provider_kind == "fixture":
        # 폐쇄 E2E: semantic-hub FixtureSha256EmbeddingProvider와 동일 알고리즘
        # (fixture-sha256-v1). 네트워크 호출 0건.
        embedding_provider = FixtureEmbeddingProvider(
            dimensions=int(os.environ.get("V2_EMBEDDING_DIMENSIONS", "16")))
    else:
        embedding_provider = HttpEmbeddingProvider()
    execution = get_runtime().execution
    # BundleExecutionContext 스키마(additionalProperties=false)와 동일한 6개 키만
    execution_context = {
        "backend": execution.backend,
        "dialect": execution.dialect,
        "catalog": execution.catalog,
        "schema_name": execution.schema,
        "identifier_quote": execution.identifier_quote,
        "qualification_pattern": execution.qualification_pattern,
    }
    return V2Deps(
        auth_config=auth_config,
        store=PostgresV2Store(pool, os.environ.get("V2_PG_SCHEMA", "public")),
        embedding_provider=embedding_provider,
        execution_context=execution_context,
        default_tenant_id=os.environ.get("V2_TENANT_ID", "kwater"),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_runtime()
    await init_metadata_repository()

    # /v2/data_decision 배선 — V2_PG_DSN이 있을 때 활성화.
    # 미설정 시 app.state.v2_deps 부재로 503 유지 (v1 무영향, fail-closed).
    # 인증(JWT)은 V2_JWKS_FILE 등이 추가로 설정된 경우에만 켜진다.
    v2_pool = None
    if os.environ.get("V2_PG_DSN"):
        import asyncpg

        v2_pool = await asyncpg.create_pool(
            dsn=os.environ["V2_PG_DSN"], min_size=1, max_size=4)
        app.state.v2_deps = _build_v2_deps(v2_pool)

    yield

    if v2_pool is not None:
        await v2_pool.close()
    await close_metadata_repository()


app = FastAPI(
    title="robo-meta-api v4",
    description=(
        "v0.7 meta-api — A안 entity resolution (resolved_entities), "
        "v0.6 8 endpoint path 유지. "
        "Semantic View v2 공급 경로 추가: `POST /v2/data_decision`은 "
        "published Semantic View Artifact 기반 Metadata Context Bundle(`meta_version:\"2\"`)을 "
        "반환한다 (폐쇄망 기본 무인증, V2_JWKS_FILE 설정 시에만 Bearer JWT 요구). "
        "`POST /query/execute`는 `artifact_id` 지정 시 해당 Artifact allowlist"
        "(테이블·컬럼·join edge·mandatory filter)로 SQL 실행을 제약한다. "
        "v1 `/data_decision` 0.7 계약은 무변경."
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
