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
    RuntimeConfigError,
    load_runtime,
)
from app.services.execution_context_resolver import (
    ExecutionBindingError,
    binding_from_store_row,
    resolve_execution_context,
    validate_runtime_bindings,
)
from app.services.query_runner_mindsdb import _validate_namespaces
from app.services.query_runner_mindsdb import execute
from app.services.sql_guard import GuardError
from app.services.sql_source_qualify import qualify_and_rewrite


class FakeRepository:
    def __init__(self, state, sources=None):
        self.state = state
        self.sources = sources if sources is not None else (
            [state] if state else []
        )

    async def list_execution_sources(self):
        return [dict(item) for item in self.sources]

    async def execution_source_scope(self, source_instance_id):
        if self.state and self.state["source_instance_id"] == source_instance_id:
            return dict(self.state)
        for item in self.sources:
            if item.get("source_instance_id") == source_instance_id:
                return dict(item)
        return None

    async def find_profile_ids_by_source_name(self, name):
        matches = []
        for item in self.sources or ([self.state] if self.state else []):
            source_name = str(item.get("source_name") or "")
            if source_name == name:
                matches.append(str(item["source_instance_id"]))
        if matches:
            return matches
        lowered = name.lower()
        for item in self.sources or ([self.state] if self.state else []):
            source_name = str(item.get("source_name") or "")
            if source_name.lower() == lowered:
                matches.append(str(item["source_instance_id"]))
        return matches

    async def find_profile_ids_by_mindsdb_catalog(self, catalog):
        matches = []
        for item in self.sources or ([self.state] if self.state else []):
            value = str(item.get("mindsdb_catalog") or "")
            if value == catalog:
                matches.append(str(item["source_instance_id"]))
        if matches:
            return matches
        lowered = catalog.lower()
        for item in self.sources or ([self.state] if self.state else []):
            value = str(item.get("mindsdb_catalog") or "")
            if value.lower() == lowered:
                matches.append(str(item["source_instance_id"]))
        return matches


def _runtime(audit_log_path: str = "test-audit.jsonl") -> RoboRuntime:
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
            default_timeout_seconds=1,
            maximum_timeout_seconds=2,
            default_max_rows=10,
            maximum_rows=20,
            maximum_response_bytes=1000,
            audit_log_path=audit_log_path,
        ),
    )


def _state(**overrides):
    value = {
        "source_instance_id": "source-tibero",
        "engine": "tibero",
        "source_schema": "GIOS_TEST",
        "mindsdb_integration": "tibero_active",
        "mindsdb_catalog": "tibero_active",
        "source_name": "GIOS",
        "allowed_objects": ["TABLE_A", "TABLE_B"],
        "allowed_schemas": ["GIOS_TEST"],
        "allowed_object_refs": [
            {"schema_name": "GIOS_TEST", "original_name": "TABLE_A"},
            {"schema_name": "GIOS_TEST", "original_name": "TABLE_B"},
        ],
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

    async def test_missing_source_id_fails_even_when_one_source_exists(self):
        with self.assertRaisesRegex(ExecutionBindingError, "source_instance_id"):
            await resolve_execution_context(FakeRepository(_state()))

    async def test_store_row_builds_binding_and_resolves(self):
        resolved = await resolve_execution_context(
            FakeRepository(_state()),
            source_instance_id="source-tibero",
        )
        self.assertEqual(resolved.source_engine, "tibero")
        self.assertEqual(resolved.parser_dialect, "mysql")
        self.assertEqual(resolved.integration, "tibero_active")
        self.assertEqual(resolved.source_name, "GIOS")
        self.assertEqual(resolved.allowed_objects, frozenset({"TABLE_A", "TABLE_B"}))
        self.assertEqual(resolved.allowed_schemas, frozenset({"gios_test"}))
        self.assertTrue(resolved.require_quoted_uppercase_identifiers)

    async def test_null_mindsdb_fields_fail_closed(self):
        with self.assertRaisesRegex(ExecutionBindingError, "mindsdb_integration"):
            await resolve_execution_context(
                FakeRepository(_state(mindsdb_integration="", mindsdb_catalog="")),
                source_instance_id="source-tibero",
            )

    async def test_integration_catalog_mismatch_fails(self):
        with self.assertRaisesRegex(ExecutionBindingError, "일치하지 않습니다"):
            binding_from_store_row(
                _state(mindsdb_integration="a", mindsdb_catalog="b")
            )

    async def test_startup_validation_allows_empty_sources(self):
        results = await validate_runtime_bindings(FakeRepository(None, sources=[]))
        self.assertEqual(results, [])

    async def test_startup_validation_lists_sources_without_active_object_check(self):
        results = await validate_runtime_bindings(
            FakeRepository(_state(allowed_objects=[]))
        )
        self.assertEqual(results[0]["source_instance_id"], "source-tibero")
        self.assertEqual(results[0]["source_name"], "GIOS")

    async def test_empty_allowlist_fails_on_resolve(self):
        with self.assertRaisesRegex(ExecutionBindingError, "활성·승인"):
            await resolve_execution_context(
                FakeRepository(_state(allowed_objects=[])),
                source_instance_id="source-tibero",
            )

    async def test_public_catalog_is_source_name_audit_keeps_mindsdb(self):
        resolved = await resolve_execution_context(
            FakeRepository(_state()),
            source_instance_id="source-tibero",
        )
        public = resolved.public_dict()
        audit = resolved.audit_dict()
        self.assertEqual(public["catalog"], "GIOS")
        self.assertEqual(public["integration"], "GIOS")
        self.assertEqual(public["source_name"], "GIOS")
        self.assertEqual(audit["catalog"], "tibero_active")
        self.assertEqual(audit["integration"], "tibero_active")

    async def test_dual_accept_claim_source_name_and_mindsdb(self):
        resolved = await resolve_execution_context(
            FakeRepository(_state()),
            source_instance_id="source-tibero",
            requested_objects=["TABLE_A"],
        )
        claim = resolved.public_dict()
        again = await resolve_execution_context(
            FakeRepository(_state()),
            claimed_context=claim,
        )
        self.assertEqual(again.catalog, "tibero_active")

        claim_mindsdb = dict(claim)
        claim_mindsdb["catalog"] = "tibero_active"
        claim_mindsdb["integration"] = "tibero_active"
        again2 = await resolve_execution_context(
            FakeRepository(_state()),
            claimed_context=claim_mindsdb,
        )
        self.assertEqual(again2.source_name, "GIOS")

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

    async def test_resolve_by_source_name_from_sql_key(self):
        resolved = await resolve_execution_context(
            FakeRepository(_state()),
            sql_source_key="GIOS",
            allow_sql_source_resolve=True,
        )
        self.assertEqual(resolved.source_instance_id, "source-tibero")

    async def test_no_source_name_blocks_public_dict(self):
        resolved = await resolve_execution_context(
            FakeRepository(_state(source_name=None)),
            source_instance_id="source-tibero",
        )
        self.assertIsNone(resolved.source_name)
        with self.assertRaisesRegex(ExecutionBindingError, "source_name"):
            resolved.public_dict()

    async def test_tibero_rewrite_then_namespace_validate(self):
        resolved = await resolve_execution_context(
            FakeRepository(_state()),
            source_instance_id="source-tibero",
            requested_objects=["TABLE_A"],
        )
        rewritten = qualify_and_rewrite(
            "SELECT * FROM `GIOS`.`GIOS_TEST`.`TABLE_A`",
            execution_context=resolved,
        )
        self.assertIn("`tibero_active`", rewritten)
        self.assertNotIn("`GIOS_TEST`", rewritten)
        _validate_namespaces(rewritten, resolved)
        with self.assertRaises(GuardError):
            qualify_and_rewrite(
                "SELECT * FROM `other`.`GIOS_TEST`.`TABLE_A`",
                execution_context=resolved,
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
        self.assertEqual(entry["execution_context"]["catalog"], "tibero_active")


class RuntimeYamlBanTests(unittest.TestCase):
    def test_source_bindings_in_yaml_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
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
    default_source_instance_id: "abc"
    source_bindings: {"abc": {"integration": "i"}}
    default_timeout_seconds: 1
    maximum_timeout_seconds: 2
    default_max_rows: 1
    maximum_rows: 2
    maximum_response_bytes: 10
    audit_log_path: ./a.jsonl
""".strip(),
                encoding="utf-8",
            )
            import os

            os.environ["METADATA_PG_PASSWORD"] = "pw"
            with self.assertRaisesRegex(RuntimeConfigError, "hardcode"):
                load_runtime(path)


if __name__ == "__main__":
    unittest.main()
