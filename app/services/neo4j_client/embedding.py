"""
임베딩 클라이언트 — K-AIR robo-data-text2sql/app/core/embedding.py 복사.
OpenAI text-embedding-3-small 사용.
"""
from __future__ import annotations

from typing import List

from openai import AsyncOpenAI

from .config import settings


class EmbeddingClient:
    """OpenAI 임베딩 생성 클라이언트 (K-AIR 원본 구조)"""

    def __init__(self, client: AsyncOpenAI | None = None):
        embed_base = settings.openai_embedding_base_url or settings.openai_base_url
        self.client = client or AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=embed_base or None,
        )
        self.model = settings.embedding_model

    async def embed_text(self, text: str) -> List[float]:
        response = await self.client.embeddings.create(
            model=self.model,
            input=text,
            encoding_format="float",
        )
        return response.data[0].embedding

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        response = await self.client.embeddings.create(
            model=self.model,
            input=texts,
            encoding_format="float",
        )
        return [item.embedding for item in response.data]

    @staticmethod
    def format_table_text(
        table_name: str, description: str = "", columns: List[str] | None = None
    ) -> str:
        parts = [f"Table: {table_name}"]
        if description:
            parts.append(f"Description: {description}")
        if columns:
            parts.append(f"Columns: {', '.join(columns)}")
        return " | ".join(parts)

    @staticmethod
    def format_column_text(
        column_name: str, table_name: str, dtype: str, description: str = ""
    ) -> str:
        parts = [f"Column: {table_name}.{column_name}", f"Type: {dtype}"]
        if description:
            parts.append(f"Description: {description}")
        return " | ".join(parts)


_client: EmbeddingClient | None = None


def get_embedding_client() -> EmbeddingClient:
    global _client
    if _client is None:
        _client = EmbeddingClient()
    return _client
