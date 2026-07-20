from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app import runtime_config
from app.runtime_config import (
    DecisionRuntime,
    EmbeddingRuntime,
    ExecutionRuntime,
    MetadataRuntime,
    RoboRuntime,
)
from app.services.embedding_provider import HttpEmbeddingProvider


class HttpEmbeddingProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous = runtime_config._runtime
        runtime_config._runtime = RoboRuntime(
            settings_path=Path("test.yaml"),
            api_host="127.0.0.1",
            api_port=8100,
            metadata_backend="postgres",
            metadata=MetadataRuntime("host", 5432, "db", "public", "u", "p"),
            embedding=EmbeddingRuntime(
                "http://embedding.test/v1",
                "text-embedding-3-small",
                1024,
                "bearer",
                "test-key",
                5,
            ),
            decision=DecisionRuntime(3, 3, 0.0, 1.0, 0.7),
            execution=ExecutionRuntime(
                "mindsdb",
                "http://mindsdb.test",
                "source",
                "source",
                "schema",
                "mysql",
                "{catalog}.{table}",
                "`",
                False,
                frozenset({"source"}),
                frozenset({"schema"}),
                1,
                2,
                10,
                20,
                1000,
                "audit.jsonl",
            ),
        )

    def tearDown(self) -> None:
        runtime_config._runtime = self.previous

    async def test_openai_payload_requests_1024_dimensions(self) -> None:
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": [{"embedding": [0.0] * 1024}]
        }
        client = MagicMock()
        client.post = AsyncMock(return_value=response)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=None)
        with patch(
            "app.services.embedding_provider.httpx.AsyncClient",
            return_value=context,
        ):
            vector = await HttpEmbeddingProvider().embed("질문")
        self.assertEqual(len(vector), 1024)
        payload = client.post.await_args.kwargs["json"]
        self.assertEqual(payload["dimensions"], 1024)


if __name__ == "__main__":
    unittest.main()
