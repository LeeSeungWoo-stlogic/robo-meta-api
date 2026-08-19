from __future__ import annotations

import json
import unittest
from dataclasses import replace
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
from app.services.query_analysis import QueryAnalyzer


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
                backend="mindsdb",
                sql_api_url="http://mindsdb.test",
                default_timeout_seconds=1,
                maximum_timeout_seconds=2,
                default_max_rows=10,
                maximum_rows=20,
                maximum_response_bytes=1000,
                audit_log_path="audit.jsonl",
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

    async def test_query_analysis_uses_separate_chat_base_url(self) -> None:
        runtime_config._runtime = replace(
            runtime_config._runtime,
            decision=replace(
                runtime_config._runtime.decision,
                analysis_base_url="http://chat.test/v1",
            ),
        )
        response = MagicMock()
        response.choices = [
            MagicMock(
                message=MagicMock(
                    content=json.dumps(
                        {
                            "intent": "사업장 조회",
                            "schema_roles": [
                                {
                                    "role": "사업장 마스터",
                                    "necessity": "required",
                                    "cardinality": "many",
                                    "search_terms": ["사업장 코드"],
                                }
                            ],
                        },
                        ensure_ascii=False,
                    )
                )
            )
        ]
        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=response)
        with patch(
            "app.services.query_analysis.AsyncOpenAI",
            return_value=client,
        ) as client_factory:
            analysis = await QueryAnalyzer().analyze("사업장을 보여줘")
        self.assertEqual(analysis.status, "complete")
        client_factory.assert_called_once_with(
            api_key="test-key",
            base_url="http://chat.test/v1",
            timeout=15.0,
            max_retries=0,
        )


if __name__ == "__main__":
    unittest.main()
