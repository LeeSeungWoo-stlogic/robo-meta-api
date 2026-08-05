from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.execution_context_resolver import ResolvedExecutionContext
from app.services.sql_guard import GuardError
from app.services.sql_source_qualify import (
    assert_single_sql_source,
    qualify_and_rewrite,
)


def _ctx(
    *,
    source_name: str | None = "GIOS",
    schemas: frozenset[str] | None = None,
    refs: frozenset[tuple[str, str]] | None = None,
    objects: frozenset[str] | None = None,
    require_quoted_uppercase: bool = True,
) -> ResolvedExecutionContext:
    schema_set = schemas or frozenset({"gios_test"})
    object_refs = refs or frozenset({("GIOS_TEST", "TABLE_A"), ("GIOS_TEST", "TABLE_B")})
    allowed_objects = objects or frozenset({"TABLE_A", "TABLE_B"})
    return ResolvedExecutionContext(
        source_instance_id="source-tibero",
        backend="mindsdb",
        integration="tibero_active",
        catalog="tibero_active",
        schema_name="GIOS_TEST",
        source_engine="tibero",
        parser_dialect="mysql",
        qualification_pattern="{catalog}.{table}",
        identifier_quote="`",
        require_quoted_uppercase_identifiers=require_quoted_uppercase,
        allowed_catalogs=frozenset({"tibero_active"}),
        allowed_schemas=schema_set,
        allowed_objects=allowed_objects,
        source_name=source_name,
        allowed_object_refs=object_refs,
    )


class SqlSourceQualifyTests(unittest.TestCase):
    def test_three_part_rewrites_to_mindsdb_two_part(self) -> None:
        rewritten = qualify_and_rewrite(
            "SELECT * FROM `GIOS`.`GIOS_TEST`.`TABLE_A`",
            execution_context=_ctx(),
        )
        self.assertIn("`tibero_active`.`TABLE_A`", rewritten)
        self.assertNotIn("GIOS_TEST", rewritten)

    def test_two_part_source_table_when_single_schema(self) -> None:
        rewritten = qualify_and_rewrite(
            "SELECT * FROM `GIOS`.`TABLE_A`",
            execution_context=_ctx(),
        )
        self.assertIn("`tibero_active`.`TABLE_A`", rewritten)

    def test_two_part_rejected_when_multiple_schemas(self) -> None:
        context = _ctx(
            schemas=frozenset({"schema_a", "schema_b"}),
            refs=frozenset(
                {
                    ("SCHEMA_A", "TABLE_A"),
                    ("SCHEMA_B", "TABLE_A"),
                }
            ),
        )
        with self.assertRaisesRegex(GuardError, "3단"):
            qualify_and_rewrite(
                "SELECT * FROM `GIOS`.`TABLE_A`",
                execution_context=context,
            )

    def test_three_part_passes_with_multiple_schemas(self) -> None:
        context = _ctx(
            schemas=frozenset({"schema_a", "schema_b"}),
            refs=frozenset(
                {
                    ("SCHEMA_A", "TABLE_A"),
                    ("SCHEMA_B", "TABLE_B"),
                }
            ),
            objects=frozenset({"TABLE_A", "TABLE_B"}),
        )
        rewritten = qualify_and_rewrite(
            "SELECT * FROM `GIOS`.`SCHEMA_A`.`TABLE_A`",
            execution_context=context,
        )
        self.assertIn("`tibero_active`.`TABLE_A`", rewritten)

    def test_kair_catalog_compat(self) -> None:
        rewritten = qualify_and_rewrite(
            "SELECT * FROM `tibero_active`.`TABLE_A`",
            execution_context=_ctx(),
        )
        self.assertIn("`tibero_active`.`TABLE_A`", rewritten)

    def test_ambiguous_or_unknown_source_rejected(self) -> None:
        with self.assertRaises(GuardError):
            qualify_and_rewrite(
                "SELECT * FROM `UNKNOWN`.`GIOS_TEST`.`TABLE_A`",
                execution_context=_ctx(),
            )

    def test_heterogeneous_sources_rejected(self) -> None:
        with self.assertRaisesRegex(GuardError, "이종 소스"):
            assert_single_sql_source(
                "SELECT * FROM `GIOS`.`GIOS_TEST`.`TABLE_A` a "
                "JOIN `RWIS`.`S`.`T` b ON 1=1"
            )


if __name__ == "__main__":
    unittest.main()
