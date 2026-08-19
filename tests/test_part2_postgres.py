from __future__ import annotations

import sys
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
)
from app.services.execution_context_resolver import ResolvedExecutionContext
from app.services.query_runner_mindsdb import _validate_namespaces
from app.services.sql_guard import GuardError
from app.services.sql_source_qualify import qualify_and_rewrite


def _ctx(
    *,
    require_quoted_uppercase: bool = False,
    allowed_objects: frozenset[str] | None = None,
) -> ResolvedExecutionContext:
    objects = allowed_objects or frozenset({"SAMPLE_TABLE"})
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
        require_quoted_uppercase_identifiers=require_quoted_uppercase,
        allowed_catalogs=frozenset({"allowed_catalog"}),
        allowed_schemas=frozenset({"allowed_schema"}),
        allowed_objects=objects,
        source_name="MySource",
        allowed_object_refs=frozenset({("allowed_schema", "SAMPLE_TABLE")}),
    )


def runtime() -> RoboRuntime:
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
            default_timeout_seconds=1,
            maximum_timeout_seconds=2,
            default_max_rows=10,
            maximum_rows=20,
            maximum_response_bytes=1000,
            audit_log_path=str(ROOT / "synthetic-audit.jsonl"),
        ),
    )


class MindsDbNamespaceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous = runtime_config._runtime
        runtime_config._runtime = runtime()

    def tearDown(self) -> None:
        runtime_config._runtime = self.previous

    def test_fully_qualified_allowed_table_passes(self) -> None:
        rewritten = qualify_and_rewrite(
            "SELECT * FROM `MySource`.`allowed_schema`.`SAMPLE_TABLE`",
            execution_context=_ctx(),
        )
        _validate_namespaces(rewritten, _ctx())
        _validate_namespaces(
            "SELECT * FROM `allowed_catalog`.`SAMPLE_TABLE`",
            _ctx(),
        )

    def test_unqualified_table_is_rejected(self) -> None:
        with self.assertRaises(GuardError):
            _validate_namespaces("SELECT * FROM SAMPLE_TABLE", _ctx())

    def test_other_catalog_is_rejected(self) -> None:
        with self.assertRaises(GuardError):
            _validate_namespaces(
                "SELECT * FROM `other_catalog`.`SAMPLE_TABLE`",
                _ctx(),
            )

    def test_cte_alias_is_not_treated_as_external_table(self) -> None:
        rewritten = qualify_and_rewrite(
            "WITH sample AS ("
            "SELECT * FROM `MySource`.`allowed_schema`.`SAMPLE_TABLE`"
            ") SELECT * FROM sample",
            execution_context=_ctx(),
        )
        _validate_namespaces(rewritten, _ctx())

    def test_execution_context_restricts_table_allowlist(self) -> None:
        context = _ctx()
        rewritten = qualify_and_rewrite(
            "SELECT * FROM `MySource`.`allowed_schema`.`SAMPLE_TABLE`",
            execution_context=context,
        )
        _validate_namespaces(rewritten, context)
        with self.assertRaises(GuardError):
            qualify_and_rewrite(
                "SELECT * FROM `MySource`.`allowed_schema`.`OTHER_TABLE`",
                execution_context=context,
            )

    def test_tibero_execution_context_requires_quoted_identifiers(self) -> None:
        context = _ctx(require_quoted_uppercase=True)
        rewritten = qualify_and_rewrite(
            "SELECT * FROM `MySource`.`allowed_schema`.`sample_table`",
            execution_context=context,
        )
        self.assertIn("`allowed_catalog`.`SAMPLE_TABLE`", rewritten)
        _validate_namespaces(rewritten, context)
        with self.assertRaises(GuardError):
            qualify_and_rewrite(
                "SELECT * FROM MySource.allowed_schema.SAMPLE_TABLE",
                execution_context=context,
            )

    def test_postgres_lowercase_input_rewrites_to_store_case(self) -> None:
        context = _ctx(
            require_quoted_uppercase=False,
            allowed_objects=frozenset({"RDITAG_TB"}),
        )
        context = ResolvedExecutionContext(
            source_instance_id=context.source_instance_id,
            backend=context.backend,
            integration=context.integration,
            catalog=context.catalog,
            schema_name="RWIS",
            source_engine=context.source_engine,
            parser_dialect=context.parser_dialect,
            qualification_pattern=context.qualification_pattern,
            identifier_quote=context.identifier_quote,
            require_quoted_uppercase_identifiers=False,
            allowed_catalogs=context.allowed_catalogs,
            allowed_schemas=frozenset({"rwis"}),
            allowed_objects=frozenset({"RDITAG_TB"}),
            source_name="RWIS",
            allowed_object_refs=frozenset({("RWIS", "RDITAG_TB")}),
        )
        rewritten = qualify_and_rewrite(
            "SELECT * FROM RWIS.rditag_tb LIMIT 5",
            execution_context=context,
        )
        self.assertIn("`allowed_catalog`.`rditag_tb`", rewritten)
        _validate_namespaces(rewritten, context)


if __name__ == "__main__":
    unittest.main()
