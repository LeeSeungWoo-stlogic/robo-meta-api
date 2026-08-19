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
    compact_public_sql,
    fold_quoted_idents_lower,
    qualify_and_rewrite,
    to_source_name_sql,
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

    def test_postgres_rewrites_lowercase_table_to_store_case(self) -> None:
        context = _ctx(
            require_quoted_uppercase=False,
            schemas=frozenset({"rwis"}),
            refs=frozenset({("RWIS", "RDITAG_TB")}),
            objects=frozenset({"RDITAG_TB"}),
            source_name="RWIS",
        )
        context = ResolvedExecutionContext(
            source_instance_id=context.source_instance_id,
            backend=context.backend,
            integration="kair_pg",
            catalog="kair_pg",
            schema_name="RWIS",
            source_engine="postgresql",
            parser_dialect=context.parser_dialect,
            qualification_pattern=context.qualification_pattern,
            identifier_quote=context.identifier_quote,
            require_quoted_uppercase_identifiers=False,
            allowed_catalogs=frozenset({"kair_pg"}),
            allowed_schemas=frozenset({"rwis"}),
            allowed_objects=frozenset({"RDITAG_TB"}),
            source_name="RWIS",
            allowed_object_refs=frozenset({("RWIS", "RDITAG_TB")}),
        )
        rewritten = qualify_and_rewrite(
            "SELECT * FROM RWIS.rditag_tb LIMIT 5",
            execution_context=context,
        )
        self.assertIn("`kair_pg`.`rditag_tb`", rewritten)
        self.assertNotIn("`RDITAG_TB`", rewritten)

    def test_tibero_quoted_lowercase_normalizes_to_store_upper(self) -> None:
        rewritten = qualify_and_rewrite(
            "SELECT * FROM `GIOS`.`GIOS_TEST`.`table_a`",
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

    def test_public_sql_uses_source_name_not_mindsdb_catalog(self) -> None:
        public = to_source_name_sql(
            "SELECT * FROM `GIOS`.`GIOS_TEST`.`TABLE_A` LIMIT 5",
            execution_context=_ctx(),
        )
        self.assertIn("`GIOS`.`GIOS_TEST`.`TABLE_A`", public)
        self.assertNotIn("tibero_active", public)

    def test_public_sql_rewrites_mindsdb_catalog_to_source_name(self) -> None:
        public = to_source_name_sql(
            "SELECT * FROM `tibero_active`.`TABLE_A` LIMIT 5",
            execution_context=_ctx(),
        )
        self.assertIn("`GIOS`", public)
        self.assertNotIn("tibero_active", public)

    def test_public_sql_postgres_mindsdb_catalog_to_source_name(self) -> None:
        ctx = ResolvedExecutionContext(
            source_instance_id="source-pg",
            backend="mindsdb",
            integration="kair_fe598447_0aca_478b_bbc8_63db85c3fa85",
            catalog="kair_fe598447_0aca_478b_bbc8_63db85c3fa85",
            schema_name="rwis",
            source_engine="postgres",
            parser_dialect="mysql",
            qualification_pattern="{catalog}.{table}",
            identifier_quote="`",
            require_quoted_uppercase_identifiers=False,
            allowed_catalogs=frozenset(
                {"kair_fe598447_0aca_478b_bbc8_63db85c3fa85"}
            ),
            allowed_schemas=frozenset({"rwis"}),
            allowed_objects=frozenset({"rditag_tb"}),
            source_name="test_rwis",
            allowed_object_refs=frozenset({("rwis", "rditag_tb")}),
        )
        public = to_source_name_sql(
            "SELECT * FROM `kair_fe598447_0aca_478b_bbc8_63db85c3fa85`"
            ".`rditag_tb` LIMIT 5",
            execution_context=ctx,
        )
        self.assertIn("`test_rwis`", public)
        self.assertIn("`rwis`.`rditag_tb`", public)
        self.assertNotIn("kair_fe598447", public)

    def test_fold_quoted_idents_lower_keeps_source_name(self) -> None:
        folded = fold_quoted_idents_lower(
            "SELECT `SUJ_NAME`, `SUJ_CODE` FROM `test_rwis`.`rwis`.`rdisaup_tb` "
            "WHERE `SUJ_NAME` LIKE '%화성정수장%'",
            keep_names={"test_rwis"},
        )
        self.assertIn("`suj_name`", folded)
        self.assertIn("`rdisaup_tb`", folded)
        self.assertIn("`test_rwis`", folded)
        self.assertNotIn("`SUJ_NAME`", folded)

    def test_postgres_rewrite_folds_quoted_column_case(self) -> None:
        ctx = ResolvedExecutionContext(
            source_instance_id="source-pg",
            backend="mindsdb",
            integration="kair_fe598447_0aca_478b_bbc8_63db85c3fa85",
            catalog="kair_fe598447_0aca_478b_bbc8_63db85c3fa85",
            schema_name="rwis",
            source_engine="postgres",
            parser_dialect="mysql",
            qualification_pattern="{catalog}.{table}",
            identifier_quote="`",
            require_quoted_uppercase_identifiers=False,
            allowed_catalogs=frozenset(
                {"kair_fe598447_0aca_478b_bbc8_63db85c3fa85", "test_rwis"}
            ),
            allowed_schemas=frozenset({"rwis"}),
            allowed_objects=frozenset({"rdisaup_tb"}),
            source_name="test_rwis",
            allowed_object_refs=frozenset({("rwis", "rdisaup_tb")}),
        )
        rewritten = qualify_and_rewrite(
            "SELECT `SUJ_NAME` FROM `test_rwis`.`rwis`.`rdisaup_tb`",
            execution_context=ctx,
        )
        self.assertIn("`suj_name`", rewritten)
        self.assertNotIn("`SUJ_NAME`", rewritten)
        self.assertIn("`kair_fe598447_0aca_478b_bbc8_63db85c3fa85`.`rdisaup_tb`", rewritten)

    def test_alias_column_is_quoted_and_folded_lower(self) -> None:
        ctx = ResolvedExecutionContext(
            source_instance_id="source-pg",
            backend="mindsdb",
            integration="kair_fe598447_0aca_478b_bbc8_63db85c3fa85",
            catalog="kair_fe598447_0aca_478b_bbc8_63db85c3fa85",
            schema_name="rwis",
            source_engine="postgres",
            parser_dialect="mysql",
            qualification_pattern="{catalog}.{table}",
            identifier_quote="`",
            require_quoted_uppercase_identifiers=False,
            allowed_catalogs=frozenset(
                {"kair_fe598447_0aca_478b_bbc8_63db85c3fa85", "test_rwis"}
            ),
            allowed_schemas=frozenset({"rwis"}),
            allowed_objects=frozenset({"rdisaup_tb"}),
            source_name="test_rwis",
            allowed_object_refs=frozenset({("rwis", "rdisaup_tb")}),
        )
        rewritten = qualify_and_rewrite(
            "SELECT f.TAGSN FROM `test_rwis`.`rwis`.`rdisaup_tb` AS f",
            execution_context=ctx,
        )
        self.assertIn("`tagsn`", rewritten)
        self.assertNotIn("`TAGSN`", rewritten)
        self.assertNotRegex(rewritten, r"(?i)(?<!`)TAGSN(?!`)")

    def test_postgres_folds_store_ident_case_and_strips_column_quals(self) -> None:
        ctx = ResolvedExecutionContext(
            source_instance_id="source-pg",
            backend="mindsdb",
            integration="kair_pg",
            catalog="kair_pg",
            schema_name="RWIS",
            source_engine="postgresql",
            parser_dialect="mysql",
            qualification_pattern="{catalog}.{table}",
            identifier_quote="`",
            require_quoted_uppercase_identifiers=False,
            allowed_catalogs=frozenset({"kair_pg"}),
            allowed_schemas=frozenset({"rwis"}),
            allowed_objects=frozenset({"RDITAG_TB"}),
            source_name="RWIS",
            allowed_object_refs=frozenset({("RWIS", "RDITAG_TB")}),
        )
        rewritten = qualify_and_rewrite(
            "SELECT `RWIS`.`RWIS`.`rditag_tb`.`SUJ_NAME` "
            "FROM `RWIS`.`RWIS`.`rditag_tb`",
            execution_context=ctx,
        )
        self.assertIn("`kair_pg`.`rditag_tb`.`suj_name`", rewritten)
        self.assertIn("`kair_pg`.`rditag_tb`", rewritten)
        self.assertNotIn("`RDITAG_TB`", rewritten)
        self.assertNotIn("`SUJ_NAME`", rewritten)
        self.assertNotIn("`RWIS`.`RWIS`.`RDITAG_TB`.`SUJ_NAME`", rewritten)

    def test_compact_public_sql_aliases_and_collapses_single_schema(self) -> None:
        ctx = ResolvedExecutionContext(
            source_instance_id="source-pg",
            backend="mindsdb",
            integration="kair_pg",
            catalog="kair_pg",
            schema_name="RWIS",
            source_engine="postgresql",
            parser_dialect="mysql",
            qualification_pattern="{catalog}.{table}",
            identifier_quote="`",
            require_quoted_uppercase_identifiers=False,
            allowed_catalogs=frozenset({"kair_pg"}),
            allowed_schemas=frozenset({"rwis"}),
            allowed_objects=frozenset({"RDISAUP_TB", "RDITAG_TB"}),
            source_name="RWIS",
            allowed_object_refs=frozenset(
                {("RWIS", "RDISAUP_TB"), ("RWIS", "RDITAG_TB")}
            ),
        )
        compact = compact_public_sql(
            "SELECT DISTINCT `RWIS`.`RWIS`.`RDISAUP_TB`.`SUJ_NAME` "
            "FROM `RWIS`.`RWIS`.`RDISAUP_TB` "
            "JOIN `RWIS`.`RWIS`.`RDITAG_TB` "
            "ON `RWIS`.`RWIS`.`RDITAG_TB`.`BNB_CODE` "
            "= `RWIS`.`RWIS`.`RDISAUP_TB`.`BNB_CODE` "
            "WHERE `RWIS`.`RWIS`.`RDITAG_TB`.`SUJ_CODE` = '354'",
            execution_context=ctx,
        )
        normalized = " ".join(compact.split())
        self.assertEqual(
            normalized,
            "SELECT DISTINCT t1.`SUJ_NAME` FROM `RWIS`.`RDISAUP_TB` AS t1 "
            "JOIN `RWIS`.`RDITAG_TB` AS t2 ON t2.`BNB_CODE` = t1.`BNB_CODE` "
            "WHERE t2.`SUJ_CODE` = '354'",
        )
        rewritten = qualify_and_rewrite(compact, execution_context=ctx)
        self.assertIn("`kair_pg`.`rdisaup_tb`", rewritten)
        self.assertIn("`suj_name`", rewritten)
        self.assertNotIn("`SUJ_NAME`", rewritten)
        self.assertNotIn("`RWIS`.`RWIS`", rewritten)

    def test_alias_string_column_is_promoted_to_identifier(self) -> None:
        ctx = ResolvedExecutionContext(
            source_instance_id="source-pg",
            backend="mindsdb",
            integration="kair_pg",
            catalog="kair_pg",
            schema_name="RWIS",
            source_engine="postgresql",
            parser_dialect="mysql",
            qualification_pattern="{catalog}.{table}",
            identifier_quote="`",
            require_quoted_uppercase_identifiers=False,
            allowed_catalogs=frozenset({"kair_pg"}),
            allowed_schemas=frozenset({"rwis"}),
            allowed_objects=frozenset({"rdd01mm_tb", "rdisaup_tb"}),
            source_name="RWIS_KT",
            allowed_object_refs=frozenset(
                {("rwis", "rdd01mm_tb"), ("rwis", "rdisaup_tb")}
            ),
        )
        sql = (
            "SELECT `p`.'SUJ_NAME', AVG(`f`.'VAL') AS `avg_val` "
            "FROM `RWIS_KT`.`rdd01mm_tb` AS `f` "
            "JOIN `RWIS_KT`.`rdisaup_tb` AS `p` "
            "ON `p`.`SUJ_CODE` = `f`.`SUJ_CODE` "
            "WHERE `p`.`SUJ_CODE` = '380'"
        )
        rewritten = qualify_and_rewrite(sql, execution_context=ctx)
        self.assertNotIn("'suj_name'", rewritten)
        self.assertNotIn("'val'", rewritten)
        self.assertIn("`suj_name`", rewritten)
        self.assertIn("`val`", rewritten)
        compact = compact_public_sql(sql, execution_context=ctx)
        self.assertNotIn("'SUJ_NAME'", compact)
        self.assertNotIn("'VAL'", compact)
        self.assertIn("`SUJ_NAME`", compact)
        self.assertIn("`VAL`", compact)
        self.assertIn("'380'", compact)

    def test_compact_public_sql_keeps_three_part_when_multiple_schemas(self) -> None:
        compact = compact_public_sql(
            "SELECT `GIOS`.`SCHEMA_A`.`TABLE_A`.`COL` "
            "FROM `GIOS`.`SCHEMA_A`.`TABLE_A`",
            execution_context=_ctx(
                schemas=frozenset({"schema_a", "schema_b"}),
                refs=frozenset(
                    {("SCHEMA_A", "TABLE_A"), ("SCHEMA_B", "TABLE_B")}
                ),
                objects=frozenset({"TABLE_A", "TABLE_B"}),
            ),
        )
        normalized = " ".join(compact.split())
        self.assertIn("`GIOS`.`SCHEMA_A`.`TABLE_A` AS t1", normalized)
        self.assertIn("t1.`COL`", normalized)
        self.assertNotIn("`GIOS`.`SCHEMA_A`.`TABLE_A`.`COL`", normalized)


if __name__ == "__main__":
    unittest.main()
