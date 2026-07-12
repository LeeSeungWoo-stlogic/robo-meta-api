"""커넥션 / 드라이버 풀.

- Neo4j 비동기 드라이버: 메타 그래프(robo-neo4j) 조회용. 단일 인스턴스를 main lifespan 에서 init/close.
- psycopg 원천 PG 풀: K-AIR 차용 query_runner 가 import 하는 `source_conn()` 컨텍스트 매니저 제공.
  K-AIR-meta-api/app/db.py 의 source_pool 패턴을 그대로 따른다 (메타 백엔드 무관).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from neo4j import AsyncDriver, AsyncGraphDatabase
import asyncpg
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from .runtime_config import get_runtime
from .services.metadata_repository import PostgresMetadataRepository


# ---------------------------------------------------------------------------
# Neo4j 드라이버
# ---------------------------------------------------------------------------

_neo4j_driver: Optional[AsyncDriver] = None


async def init_neo4j() -> AsyncDriver:
    global _neo4j_driver
    if _neo4j_driver is None:
        from .config import settings

        _neo4j_driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    return _neo4j_driver


async def close_neo4j() -> None:
    global _neo4j_driver
    if _neo4j_driver is not None:
        await _neo4j_driver.close()
        _neo4j_driver = None


def get_neo4j() -> AsyncDriver:
    if _neo4j_driver is None:
        raise RuntimeError("neo4j driver not initialized")
    return _neo4j_driver


# ---------------------------------------------------------------------------
# 원천 PG 풀 (K-AIR query_runner 가 의존)
# ---------------------------------------------------------------------------

_source_pool: Optional[AsyncConnectionPool] = None


async def init_source_pool() -> AsyncConnectionPool:
    global _source_pool
    if _source_pool is None:
        from .config import settings

        _source_pool = AsyncConnectionPool(
            conninfo=settings.source_dsn,
            min_size=1,
            max_size=4,
            open=False,
        )
        await _source_pool.open()
    return _source_pool


async def close_source_pool() -> None:
    global _source_pool
    if _source_pool is not None:
        await _source_pool.close()
        _source_pool = None


def get_source_pool() -> AsyncConnectionPool:
    if _source_pool is None:
        raise RuntimeError("source connection pool not initialized")
    return _source_pool


@asynccontextmanager
async def source_conn() -> AsyncIterator[AsyncConnection]:
    pool = get_source_pool()
    async with pool.connection() as conn:
        yield conn


_metadata_pool: Optional[asyncpg.Pool] = None
_metadata_repository: Optional[PostgresMetadataRepository] = None


async def init_metadata_repository() -> PostgresMetadataRepository:
    global _metadata_pool, _metadata_repository
    if _metadata_repository is None:
        runtime = get_runtime()
        _metadata_pool = await asyncpg.create_pool(
            dsn=runtime.metadata.dsn,
            min_size=1,
            max_size=4,
            server_settings={"search_path": runtime.metadata.schema},
        )
        _metadata_repository = PostgresMetadataRepository(_metadata_pool, runtime)
    return _metadata_repository


async def close_metadata_repository() -> None:
    global _metadata_pool, _metadata_repository
    if _metadata_pool is not None:
        await _metadata_pool.close()
    _metadata_pool = None
    _metadata_repository = None


def get_metadata_repository() -> PostgresMetadataRepository:
    if _metadata_repository is None:
        raise RuntimeError("metadata repository not initialized")
    return _metadata_repository
