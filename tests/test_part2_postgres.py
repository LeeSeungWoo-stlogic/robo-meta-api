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
from app.services.query_runner_mindsdb import _validate_namespaces
from app.services.sql_guard import GuardError


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
            integration="allowed_catalog",
            catalog="allowed_catalog",
            schema="allowed_schema",
            dialect="mysql",
            qualification_pattern="{catalog}.{schema}.{table}",
            identifier_quote="`",
            require_quoted_uppercase_identifiers=True,
            allowed_catalogs=frozenset({"allowed_catalog"}),
            allowed_schemas=frozenset({"allowed_schema"}),
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
        _validate_namespaces(
            "SELECT * FROM `allowed_catalog`.`allowed_schema`.`SAMPLE_TABLE`"
        )

    def test_unqualified_table_is_rejected(self) -> None:
        with self.assertRaises(GuardError):
            _validate_namespaces("SELECT * FROM SAMPLE_TABLE")

    def test_other_catalog_is_rejected(self) -> None:
        with self.assertRaises(GuardError):
            _validate_namespaces(
                "SELECT * FROM `other_catalog`.`allowed_schema`.`SAMPLE_TABLE`"
            )

    def test_cte_alias_is_not_treated_as_external_table(self) -> None:
        _validate_namespaces(
            "WITH sample AS ("
            "SELECT * FROM `allowed_catalog`.`allowed_schema`.`SAMPLE_TABLE`"
            ") SELECT * FROM sample"
        )


if __name__ == "__main__":
    unittest.main()
