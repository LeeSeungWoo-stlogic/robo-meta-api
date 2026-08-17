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
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

from .db import close_metadata_repository, get_metadata_repository, init_metadata_repository
from .routers import decision, decision_v2, meta, query_exec, t2sql
from .runtime_config import get_runtime, init_runtime
from .schemas import APP_VERSION, META_VERSION
from .services.execution_context_resolver import validate_runtime_bindings


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_runtime()
    metadata_repository = await init_metadata_repository()
    await validate_runtime_bindings(metadata_repository)
    # ADR-002 Wave 2: /semantic_decision은 410 stub. V2_PG_DSN 배선 제거.
    yield
    await close_metadata_repository()


# OA path proxy (e.g. /robo). Empty locally.
_ROOT_PATH = os.getenv("ROOT_PATH", "").rstrip("/")

app = FastAPI(
    title="robo-meta-api 1.0",
    description=(
        "robo-meta-api 1.0 — Serving MVP 소비면은 "
        "`POST /data_decision`과 `POST /query_execute`다. "
        "`POST /t2sql`은 확정 SQL+used 메타 추가 소비면이다. "
        "`/data_decision` meta_version은 1.0. "
        "`/semantic_decision`·`/query`·구경로 `/query/execute`는 폐기(410 또는 미노출)."
    ),
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    root_path=_ROOT_PATH,
    openapi_tags=[
        {"name": "decision", "description": "자연어 → Metadata Context (`/data_decision`)"},
        {"name": "t2sql", "description": "자연어 → 확정 SQL + used 메타 (`/t2sql`)"},
        {"name": "query", "description": "읽기 전용 SQL 실행 (`/query_execute`)"},
        {"name": "meta", "description": "테이블·컬럼·FK 메타 조회"},
    ],
)

# Closed-network docs: local static (no CDN).
# Avoid StaticFiles mount with root_path (double strip → bare /static 404).
# Avoid FileResponse through @app.middleware(BaseHTTPMiddleware) — can 500 under
# some ASGI stacks; return buffered Response instead.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_SWAGGER_STATIC = _STATIC_DIR / "swagger"
_SWAGGER_MEDIA = {
    "swagger-ui.css": "text/css; charset=utf-8",
    "swagger-ui-bundle.js": "application/javascript; charset=utf-8",
    "swagger-ui-standalone-preset.js": "application/javascript; charset=utf-8",
    "redoc.standalone.js": "application/javascript; charset=utf-8",
}


@app.get("/static/swagger/{name}", include_in_schema=False)
def swagger_static(name: str):
    media = _SWAGGER_MEDIA.get(name)
    if media is None:
        raise HTTPException(status_code=404)
    path = _SWAGGER_STATIC / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return Response(content=path.read_bytes(), media_type=media)


@app.get("/docs", include_in_schema=False)
def swagger_ui():
    prefix = _ROOT_PATH
    return get_swagger_ui_html(
        openapi_url=f"{prefix}/openapi.json",
        title="robo-meta-api 1.0 - API docs",
        swagger_js_url=f"{prefix}/static/swagger/swagger-ui-bundle.js",
        swagger_css_url=f"{prefix}/static/swagger/swagger-ui.css",
    )


@app.get("/redoc", include_in_schema=False)
def redoc_ui():
    prefix = _ROOT_PATH
    return get_redoc_html(
        openapi_url=f"{prefix}/openapi.json",
        title="robo-meta-api 1.0 - ReDoc",
        redoc_js_url=f"{prefix}/static/swagger/redoc.standalone.js",
        with_google_fonts=False,
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
    try:
        sources = await get_metadata_repository().list_execution_sources()
    except Exception:
        response.status_code = 503
        return {
            "version": APP_VERSION,
            "meta_version": META_VERSION,
            "status": "unavailable",
            "metadata_backend": runtime.metadata_backend,
            "execution_backend": runtime.execution.backend,
            "t2sql_configured": bool(
                runtime.t2sql is not None and runtime.t2sql.configured()
            ),
            "source_binding_count": 0,
            "source_instance_ids": [],
            "detail": "Metadata Store list_execution_sources failed",
        }
    source_ids = [
        str(item.get("source_instance_id") or "")
        for item in sources
        if str(item.get("source_instance_id") or "").strip()
    ]
    return {
        "version": APP_VERSION,
        "meta_version": META_VERSION,
        "status": "ok",
        "metadata_backend": runtime.metadata_backend,
        "execution_backend": runtime.execution.backend,
        "t2sql_configured": bool(
            runtime.t2sql is not None and runtime.t2sql.configured()
        ),
        "source_binding_count": len(source_ids),
        "source_instance_ids": source_ids,
    }


app.include_router(decision.router)
app.include_router(t2sql.router)
app.include_router(query_exec.router)
app.include_router(meta.router)
# /semantic_decision — OpenAPI 미노출, 런타임 410 (ADR-002 Wave 2)
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
