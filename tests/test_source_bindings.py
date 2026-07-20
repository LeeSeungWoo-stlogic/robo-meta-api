from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


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
    SourceBindingRuntime,
)
from app.services.execution_context_resolver import (
    ExecutionBindingError,
    resolve_execution_context,
    validate_runtime_bindings,
)
from app.services.query_runner_mindsdb import _validate_namespaces
from app.services.query_runner_mindsdb import execute
from app.services.sql_guard import GuardError


class FakeRepository:
    def __init__(self, state):
        self.state = state

    async def execution_source_scope(self, source_instance_id):
        if self.state and self.state["source_instance_id"] == source_instance_id:
            return dict(self.state)
        return None


def _binding(
    source_instance_id: str = "source-tibero",
) -> SourceBindingRuntime:
    return SourceBindingRuntime(
        source_instance_id=source_instance_id,
        integration="tibero_active",
        catalog="GIOS_TEST",
        schema="GIOS_TEST",
        source_engine="tibero",
        parser_dialect="mysql",
        qualification_pattern="{catalog}.{table}",
        identifier_quote="`",
        require_quoted_uppercase_identifiers=True,
        allowed_catalogs=frozenset({"tibero_active"}),
        allowed_schemas=frozenset({"gios_test"}),
    )


def _runtime(audit_log_path: str = "test-audit.jsonl") -> RoboRuntime:
    binding = _binding()
    return RoboRuntime(
        settings_path=Path("test.yaml"),
        api_host="127.0.0.1",
        api_port=8100,
        metadata_backend="postgres",
        metadata=MetadataRuntime("host", 5432, "t2s", "public", "user", "pw"),
        embedding=EmbeddingRuntime(
            "http://embedding.invalid",
            "embedding",
            1024,
            "none",
            None,
            1,
        ),
        decision=DecisionRuntime(3, 3, 0.0, 1.0, 0.7),
        execution=ExecutionRuntime(
            backend="mindsdb",
            sql_api_url="http://mindsdb.invalid/api/sql/query",
            integration=binding.integration,
            catalog=binding.catalog,
            schema=binding.schema,
            dialect=binding.parser_dialect,
            qualification_pattern=binding.qualification_pattern,
            identifier_quote=binding.identifier_quote,
            require_quoted_uppercase_identifiers=True,
            allowed_catalogs=binding.allowed_catalogs,
            allowed_schemas=binding.allowed_schemas,
            default_timeout_seconds=1,
            maximum_timeout_seconds=2,
            default_max_rows=10,
            maximum_rows=20,
            maximum_response_bytes=1000,
            audit_log_path=audit_log_path,
            source_bindings={binding.source_instance_id: binding},
            default_source_instance_id=binding.source_instance_id,
        ),
    )


def _state(**overrides):
    value = {
        "source_instance_id": "source-tibero",
        "engine": "tibero",
        "source_schema": "GIOS_TEST",
        "mindsdb_integration": None,
        "mindsdb_catalog": None,
        "allowed_objects": ["TABLE_A", "TABLE_B"],
    }
    value.update(overrides)
    return value


class SourceBindingResolverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous = runtime_config._runtime
        self.tempdir = tempfile.TemporaryDirectory()
        self.audit_path = Path(self.tempdir.name) / "audit.jsonl"
        runtime_config._runtime = _runtime(str(self.audit_path))

    def tearDown(self):
        runtime_config._runtime = self.previous
        self.tempdir.cleanup()

    async def test_default_binding_accepts_null_legacy_columns(self):
        resolved = await resolve_execution_context(
            FakeRepository(_state()),
            allow_default=True,
        )

        self.assertEqual(resolved.source_engine, "tibero")
        self.assertEqual(resolved.parser_dialect, "mysql")
        self.assertEqual(resolved.allowed_objects, frozenset({"TABLE_A", "TABLE_B"}))

    async def test_non_null_legacy_mismatch_fails_closed(self):
        with self.assertRaisesRegex(ExecutionBindingError, "mindsdb_integration"):
            await resolve_execution_context(
                FakeRepository(_state(mindsdb_integration="wrong")),
                allow_default=True,
            )

    async def test_startup_validation_requires_active_approved_objects(self):
        results = await validate_runtime_bindings(FakeRepository(_state()))
        self.assertEqual(results[0]["active_object_count"], 2)

        with self.assertRaisesRegex(ExecutionBindingError, "활성·승인"):
            await validate_runtime_bindings(
                FakeRepository(_state(allowed_objects=[]))
            )

    async def test_spoofed_context_and_object_are_rejected(self):
        resolved = await resolve_execution_context(
            FakeRepository(_state()),
            source_instance_id="source-tibero",
            requested_objects=["TABLE_A"],
        )
        claim = resolved.public_dict()
        claim["catalog"] = "other"
        with self.assertRaisesRegex(ExecutionBindingError, "catalog"):
            await resolve_execution_context(
                FakeRepository(_state()),
                claimed_context=claim,
            )

        claim = resolved.public_dict()
        claim["allowed_objects"] = ["UNAPPROVED"]
        with self.assertRaisesRegex(ExecutionBindingError, "비활성"):
            await resolve_execution_context(
                FakeRepository(_state()),
                claimed_context=claim,
            )

    async def test_tibero_source_uses_mindsdb_mysql_parser(self):
        resolved = await resolve_execution_context(
            FakeRepository(_state()),
            source_instance_id="source-tibero",
            requested_objects=["TABLE_A"],
        )

        _validate_namespaces(
            "SELECT * FROM `tibero_active`.`GIOS_TEST`.`TABLE_A`",
            resolved,
        )
        with self.assertRaises(GuardError):
            _validate_namespaces(
                "SELECT * FROM `other`.`GIOS_TEST`.`TABLE_A`",
                resolved,
            )

    async def test_rejected_query_audit_uses_resolved_integration(self):
        resolved = await resolve_execution_context(
            FakeRepository(_state()),
            source_instance_id="source-tibero",
            requested_objects=["TABLE_A"],
        )

        with self.assertRaises(GuardError):
            await execute(
                "SELECT * FROM `other`.`GIOS_TEST`.`TABLE_A`",
                timeout_s=None,
                max_rows=None,
                execution_context=resolved,
            )
        entry = json.loads(self.audit_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(entry["integration"], "tibero_active")
        self.assertEqual(entry["resolved_integration"], "tibero_active")
        self.assertEqual(entry["execution_context"]["parser_dialect"], "mysql")


if __name__ == "__main__":
    unittest.main()
