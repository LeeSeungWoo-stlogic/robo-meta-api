from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import runtime_config
from app.runtime_config import (
    DecisionRuntime,
    EmbeddingRuntime,
    ExecutionRuntime,
    MetadataRuntime,
    RoboRuntime,
    load_runtime,
)
from app.schemas import QueryExecuteRequest
from app.services.execution_context_resolver import ResolvedExecutionContext
from app.services.query_runner_mindsdb import execute


def _ctx() -> ResolvedExecutionContext:
    return ResolvedExecutionContext(
        source_instance_id="src",
        backend="mindsdb",
        integration="allowed_catalog",
        catalog="allowed_catalog",
        schema_name="allowed_schema",
        source_engine="postgresql",
        parser_dialect="mysql",
        qualification_pattern="{catalog}.{table}",
        identifier_quote="`",
        require_quoted_uppercase_identifiers=False,
        allowed_catalogs=frozenset({"allowed_catalog"}),
        allowed_schemas=frozenset({"allowed_schema"}),
        allowed_objects=frozenset({"SAMPLE_TABLE"}),
        source_name="MySource",
        allowed_object_refs=frozenset({("allowed_schema", "SAMPLE_TABLE")}),
    )


def _runtime(audit_path: str) -> RoboRuntime:
    return RoboRuntime(
        settings_path=ROOT / "synthetic-settings.yaml",
        api_host="127.0.0.1",
        api_port=10001,
        metadata_backend="postgres",
        metadata=MetadataRuntime(
            host="metadata",
            port=10002,
            database="metadata",
            schema="public",
            user="metadata",
            password="secret",
        ),
        embedding=EmbeddingRuntime(
            base_url="http://embedding.invalid/v1",
            model="embedding-model",
            dimensions=3,
            auth_mode="none",
            api_key=None,
            timeout_seconds=1,
        ),
        decision=DecisionRuntime(
            table_top_k=3,
            column_top_m=2,
            minimum_similarity=0.0,
            verified_join_confidence=1.0,
            convention_join_confidence=0.7,
        ),
        execution=ExecutionRuntime(
            backend="mindsdb",
            sql_api_url="http://mindsdb.invalid/api/sql/query",
            default_timeout_seconds=10,
            maximum_timeout_seconds=120,
            default_max_rows=10,
            maximum_rows=20,
            maximum_response_bytes=1000,
            audit_log_path=audit_path,
        ),
    )


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    last_timeout = None

    def __init__(self, timeout=None, **_kwargs):
        _FakeClient.last_timeout = timeout
        self._response = None
        self._exc = None

    def set_result(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, *_args, **_kwargs):
        if self._exc is not None:
            raise self._exc
        return self._response


class QueryExecuteTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous = runtime_config._runtime
        self._tmp = tempfile.TemporaryDirectory()
        self.audit_path = Path(self._tmp.name) / "audit.jsonl"
        runtime_config._runtime = _runtime(str(self.audit_path))
        self._grace = os.environ.get("EXEC_ERROR_RETURN_GRACE_S")
        os.environ["EXEC_ERROR_RETURN_GRACE_S"] = "30"

    def tearDown(self):
        runtime_config._runtime = self.previous
        if self._grace is None:
            os.environ.pop("EXEC_ERROR_RETURN_GRACE_S", None)
        else:
            os.environ["EXEC_ERROR_RETURN_GRACE_S"] = self._grace
        self._tmp.cleanup()

    async def _run(self, *, response=None, exc=None, timeout_s=10):
        captured = {}

        def factory(*args, **kwargs):
            client = _FakeClient(*args, **kwargs)
            client.set_result(response=response, exc=exc)
            captured["timeout"] = kwargs.get("timeout", args[0] if args else None)
            return client

        with patch("app.services.query_runner_mindsdb.httpx.AsyncClient", factory):
            result = await execute(
                "SELECT * FROM `MySource`.`allowed_schema`.`SAMPLE_TABLE` LIMIT 1",
                timeout_s=timeout_s,
                max_rows=5,
                execution_context=_ctx(),
            )
        return result, captured

    async def test_http_error_body_is_returned_as_db_error(self):
        result, captured = await self._run(
            response=_FakeResponse(
                500,
                {
                    "type": "error",
                    "error_message": 'column "measure_month" does not exist',
                },
            )
        )
        self.assertEqual(result["status"], "db_error")
        self.assertIn("measure_month", result["error"])
        self.assertEqual(captured["timeout"], 40)

    async def test_timeout_still_reported_when_mindsdb_never_answers(self):
        result, _captured = await self._run(
            exc=httpx.ReadTimeout("slow"),
            timeout_s=10,
        )
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["error"], "MindsDB request timeout: 10s")

    async def test_request_timeout_is_clipped_to_runtime_maximum(self):
        result, captured = await self._run(
            response=_FakeResponse(
                200,
                {"type": "table", "column_names": ["id"], "data": [[1]]},
            ),
            timeout_s=180,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["timeout_s_applied"], 120)
        self.assertEqual(captured["timeout"], 150)


class QueryExecuteRequestLimitTests(unittest.TestCase):
    def test_timeout_s_180_is_accepted(self):
        req = QueryExecuteRequest(sql="SELECT 1", timeout_s=180)
        self.assertEqual(req.timeout_s, 180)

    def test_timeout_s_above_schema_cap_is_rejected(self):
        with self.assertRaises(ValidationError):
            QueryExecuteRequest(sql="SELECT 1", timeout_s=601)


class RuntimeTimeoutEnvTests(unittest.TestCase):
    def test_exec_max_timeout_env_raises_yaml_cap(self):
        previous_max = os.environ.get("EXEC_MAX_TIMEOUT_S")
        previous_default = os.environ.get("EXEC_DEFAULT_TIMEOUT_S")
        os.environ["METADATA_PG_PASSWORD"] = "pw"
        os.environ["EXEC_MAX_TIMEOUT_S"] = "300"
        os.environ["EXEC_DEFAULT_TIMEOUT_S"] = "60"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "ok.yaml"
                path.write_text(
                    """
robo_meta_api:
  api_host: "0.0.0.0"
  api_port: 8100
  metadata_backend: postgres
  metadata_store:
    host: h
    port: 5432
    database: d
    schema: public
    user: u
    password_ref: {provider: process_env, env_key: METADATA_PG_PASSWORD}
  embedding:
    base_url: http://x
    model: m
    dimensions: 8
    auth_mode: none
    timeout_seconds: 1
  decision:
    table_top_k: 1
    column_top_m: 1
    minimum_similarity: 0.0
    verified_join_confidence: 1.0
    convention_join_confidence: 0.7
  execution:
    backend: mindsdb
    sql_api_url: http://mindsdb
    default_timeout_seconds: 10
    maximum_timeout_seconds: 30
    default_max_rows: 1
    maximum_rows: 20
    maximum_response_bytes: 10
    audit_log_path: ./a.jsonl
""".strip(),
                    encoding="utf-8",
                )
                runtime = load_runtime(path)
                self.assertEqual(runtime.execution.maximum_timeout_seconds, 300)
                self.assertEqual(runtime.execution.default_timeout_seconds, 60)
        finally:
            if previous_max is None:
                os.environ.pop("EXEC_MAX_TIMEOUT_S", None)
            else:
                os.environ["EXEC_MAX_TIMEOUT_S"] = previous_max
            if previous_default is None:
                os.environ.pop("EXEC_DEFAULT_TIMEOUT_S", None)
            else:
                os.environ["EXEC_DEFAULT_TIMEOUT_S"] = previous_default


if __name__ == "__main__":
    unittest.main()
