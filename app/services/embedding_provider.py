"""EmbeddingProvider 인터페이스 (플랜 5C).

- v1 `decision_postgres._embed_question`의 하드코딩 HTTP 호출을 분리한다.
- production: HttpEmbeddingProvider (기존 v1 동작과 동일).
- 시험·deterministic E2E: FixtureEmbeddingProvider — 기준 질문만 허용,
  SHA-256 유도 고정 vector, 네트워크 호출 0건.
- provider 장애 시 zero vector·lexical-only 강등 없이 예외를 던진다 (fail-closed).
"""
from __future__ import annotations

import hashlib
import math
import re
import struct
from typing import Protocol

import httpx

from ..runtime_config import get_runtime


class EmbeddingProviderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int

    async def embed(self, text: str) -> list[float]: ...
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class HttpEmbeddingProvider:
    """runtime 설정 기반 HTTP embedding (v1 기존 동작)."""

    def __init__(self) -> None:
        runtime = get_runtime()
        self.model = runtime.embedding.model
        self.dimensions = runtime.embedding.dimensions

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        runtime = get_runtime()
        headers = {"Content-Type": "application/json"}
        if runtime.embedding.auth_mode == "bearer" and runtime.embedding.api_key:
            headers["Authorization"] = f"Bearer {runtime.embedding.api_key}"
        payload = {
            "model": runtime.embedding.model,
            "input": texts,
            "dimensions": runtime.embedding.dimensions,
        }
        try:
            async with httpx.AsyncClient(
                timeout=runtime.embedding.timeout_seconds
            ) as client:
                response = await client.post(
                    runtime.embedding.base_url.rstrip("/") + "/embeddings",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as e:
            raise EmbeddingProviderError(
                "EMBED_PROVIDER_DOWN", f"embedding provider 호출 실패: {e}") from e
        rows = sorted(body["data"], key=lambda item: int(item.get("index", 0)))
        if len(rows) != len(texts):
            raise EmbeddingProviderError(
                "EMBED_BATCH_MISMATCH",
                f"Embedding response count mismatch: {len(rows)} != {len(texts)}",
            )
        vectors = [row["embedding"] for row in rows]
        for vector in vectors:
            if len(vector) != runtime.embedding.dimensions:
                raise EmbeddingProviderError(
                    "EMBED_DIM_MISMATCH",
                    f"Question embedding dimension mismatch: {len(vector)}",
                )
        return vectors


def _fixed_embedding(text: str, dim: int) -> list[float]:
    values: list[float] = []
    counter = 0
    while len(values) < dim:
        digest = hashlib.sha256(f"{text}|{counter}".encode("utf-8")).digest()
        for i in range(0, len(digest) - 3, 4):
            if len(values) >= dim:
                break
            (u,) = struct.unpack(">I", digest[i:i + 4])
            values.append(round(u / 2**31 - 1.0, 8))
        counter += 1
    return values


class FixtureEmbeddingProvider:
    """기준 질문만 허용하는 결정적 provider (semantic-hub fixture와 동일 알고리즘)."""

    model = "fixture-sha256-v1"

    def __init__(self, dimensions: int = 16,
                 allowed_texts: set[str] | None = None) -> None:
        self.dimensions = dimensions
        self._allowed = allowed_texts

    async def embed(self, text: str) -> list[float]:
        if self._allowed is not None and text not in self._allowed:
            raise EmbeddingProviderError(
                "EMBED_TEXT_NOT_ALLOWED",
                "fixture provider는 기준 질문만 embedding할 수 있습니다.")
        return _fixed_embedding(text, self.dimensions)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(text) for text in texts]


class LexicalHashEmbeddingProvider:
    """K-AIR-meta-ingest의 비운영 1024차원 검색 회귀 provider."""

    model = "lexical-hash-test"
    dimensions = 1024

    async def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = re.findall(r"[A-Za-z0-9_]+|[가-힣]{2,}", text.lower())
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(text) for text in texts]


class FailingEmbeddingProvider:
    model = "failing-v1"
    dimensions = 16

    async def embed(self, text: str) -> list[float]:
        raise EmbeddingProviderError("EMBED_PROVIDER_DOWN", "embedding provider 장애")

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise EmbeddingProviderError("EMBED_PROVIDER_DOWN", "embedding provider 장애")


_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    global _provider
    if _provider is None:
        _provider = HttpEmbeddingProvider()
    return _provider


def set_embedding_provider(provider: EmbeddingProvider | None) -> None:
    """시험·격리 환경에서 provider를 교체한다. None이면 기본(HTTP)으로 복원."""
    global _provider
    _provider = provider
