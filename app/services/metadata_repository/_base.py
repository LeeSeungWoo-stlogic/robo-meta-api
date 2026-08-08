from __future__ import annotations

from typing import Any

import asyncpg

from ...runtime_config import RoboRuntime


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(float(item)) for item in vector) + "]"


class MetadataRepositoryBase:
    def __init__(self, pool: asyncpg.Pool, runtime: RoboRuntime):
        self._pool = pool
        self._runtime = runtime
