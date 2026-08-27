from __future__ import annotations

import json
import unittest

from app.services.meta_postgres import (
    _batch_item,
    _catalog_column,
    _column_meta,
    _table_info,
    assemble_serving_catalog,
    batch_items_from_catalog,
    refs_from_catalog,
    slice_serving_catalog,
)


def _table(**overrides: object) -> dict:
    row = {
        "id": 1,
        "db": "rwis",
        "schema_name": "RWIS",
        "original_name": "RDITAG_TB",
        "description": "태그 원본 설명",
        "analyzed_description": "태그 마스터. TAGSN으로 팩트와 조인.",
        "profile_id": "profile-1",
        "engine": "oracle",
        "metadata": {
            "logical_name": "태그 마스터",
            "subject_area": "master",
        },
    }
    row.update(overrides)
    return row


class MetaCatalogMappingTests(unittest.TestCase):
    def test_table_uses_logical_name_not_description(self) -> None:
        info = _table_info(_table(), [])
        self.assertEqual(info.table_name, "RDITAG_TB")
        self.assertEqual(info.table_name_kr, "태그 마스터")
        self.assertEqual(info.table_comment, "태그 원본 설명")
        self.assertEqual(
            info.description,
            "태그 마스터. TAGSN으로 팩트와 조인.",
        )
        self.assertEqual(info.subject_area, "master")
        self.assertEqual(info.table_type, "Dimension")

    def test_blank_logical_name_stays_empty(self) -> None:
        info = _table_info(
            _table(metadata={"logical_name": "「미정」", "subject_area": "raw"}),
            [],
        )
        self.assertIsNone(info.table_name_kr)
        self.assertEqual(info.subject_area, "raw")
        self.assertEqual(info.table_type, "Raw")

    def test_subject_area_override_wins(self) -> None:
        info = _table_info(
            _table(
                metadata={
                    "logical_name": "일 DATA",
                    "subject_area": "raw",
                    "subject_area_override": "agg",
                }
            ),
            [],
        )
        self.assertEqual(info.subject_area, "agg")
        self.assertEqual(info.table_type, "Fact")

    def test_column_maps_store_facts_like_decision(self) -> None:
        column = _column_meta(
            {
                "name": "VAL",
                "dtype": "NUMBER",
                "nullable": True,
                "is_primary_key": False,
                "is_foreign_key": False,
                "description": "측정값",
                "analyzed_description": "계측 수치",
                "logical_name": "측정값",
                "metadata": {
                    "data_type_with_length": "NUMBER(10,2)",
                    "sample_values": [
                        {"value": 1.2},
                        "2.3",
                        {"value": "3.4"},
                        "4.5",
                        "5.6",
                        "ignored",
                    ],
                    "format_pattern": "0.0",
                    "unit": "mg/L",
                    "pk_ordinal": "2",
                },
            }
        )
        self.assertEqual(column.column_name_kr, "측정값")
        self.assertEqual(column.column_comment, "측정값")
        self.assertEqual(column.description, "계측 수치")
        self.assertEqual(column.data_type, "NUMBER(10,2)")
        self.assertEqual(
            column.value_examples,
            ["1.2", "2.3", "3.4", "4.5", "5.6"],
        )
        self.assertEqual(column.format_pattern, "0.0")
        self.assertEqual(column.unit, "mg/L")
        self.assertEqual(column.pk_ordinal, 2)
        self.assertEqual(column.has_code, "N")
        self.assertIsNone(column.facility_code)
        self.assertIsNone(column.system_code)
        self.assertIsNone(column.code_lookup)

    def test_column_maps_facility_and_system_codes(self) -> None:
        column = _column_meta(
            {
                "name": "VALUE",
                "metadata": {
                    "facility_code": "FAC1",
                    "system_code": "SYS9",
                    "facility_scope": "ignored-when-facility_code-present",
                },
            }
        )
        self.assertEqual(column.facility_code, "FAC1")
        self.assertEqual(column.system_code, "SYS9")

    def test_column_name_kr_does_not_use_description(self) -> None:
        column = _column_meta(
            {
                "name": "TAG_DESC",
                "dtype": "VARCHAR2",
                "description": "태그 설명을 길게 적은 원본 코멘트",
                "metadata": {"logical_name": "태그설명"},
            }
        )
        self.assertEqual(column.column_name_kr, "태그설명")
        self.assertEqual(
            column.column_comment,
            "태그 설명을 길게 적은 원본 코멘트",
        )

    def test_fk_fills_code_lookup(self) -> None:
        column = _column_meta(
            {
                "name": "TAGSN",
                "dtype": "NUMBER",
                "is_foreign_key": True,
                "metadata": {},
            },
            refs=[
                {
                    "column_name": "TAGSN",
                    "position": 1,
                    "ref_schema_name": "RWIS",
                    "ref_table_name": "RDITAG_TB",
                    "ref_column_name": "TAGSN",
                }
            ],
        )
        self.assertEqual(column.constraints, ["FK"])
        self.assertIsNotNone(column.code_lookup)
        assert column.code_lookup is not None
        self.assertEqual(column.code_lookup.ref_table_name, "RDITAG_TB")
        self.assertEqual(column.code_lookup.ref_column_name, "TAGSN")
        self.assertEqual(column.code_lookup.via, "fk")

    def test_has_code_only_on_value_mapping_columns(self) -> None:
        code = _column_meta(
            {"name": "BR_CODE", "metadata": {}},
            code_columns={"BR_CODE"},
        )
        name = _column_meta(
            {"name": "BR_NAME", "metadata": {}},
            code_columns={"BR_CODE"},
        )
        self.assertEqual(code.has_code, "Y")
        self.assertEqual(name.has_code, "N")

    def test_default_date_column_from_format_pattern(self) -> None:
        info = _table_info(
            _table(),
            [
                {"name": "TAGSN", "dtype": "NUMBER", "metadata": {}},
                {
                    "name": "LOG_TIME",
                    "dtype": "CHAR",
                    "is_primary_key": True,
                    "metadata": {"format_pattern": "YYYYMMDDHH24MISS"},
                },
            ],
        )
        self.assertEqual(info.default_date_column, "LOG_TIME")
        self.assertEqual(info.pk_columns, ["LOG_TIME"])

    def test_batch_item_carries_catalog_fields(self) -> None:
        item = _batch_item(
            {
                "db": "rwis",
                "schema_name": "RWIS",
                "table_name": "RDITAG_TB",
                "description": "태그",
                "analyzed_description": "태그 마스터",
                "metadata": json.dumps(
                    {"logical_name": "태그 마스터", "subject_area": "master"}
                ),
            }
        )
        self.assertEqual(item.table_name_kr, "태그 마스터")
        self.assertEqual(item.table_comment, "태그")
        self.assertEqual(item.description, "태그 마스터")
        self.assertEqual(item.subject_area, "master")


class ServingCatalogTests(unittest.TestCase):
    def test_varchar_display_and_constraints(self) -> None:
        column = _catalog_column(
            {
                "column_name": "suj_code",
                "dtype": "character varying",
                "nullable": True,
                "is_primary_key": False,
                "metadata": {"data_type_with_length": "character varying(10)"},
            }
        )
        self.assertEqual(column.column_name, "suj_code")
        self.assertEqual(column.data_type, "varchar(10)")
        self.assertTrue(column.nullable)
        self.assertFalse(column.primary_key)
        self.assertIsNone(column.references)
        self.assertIsNone(column.comment)
        self.assertIsNone(column.description)
        self.assertIsNone(column.referenced_by)

    def test_outbound_fk_becomes_references(self) -> None:
        column = _catalog_column(
            {
                "column_name": "tagsn",
                "dtype": "numeric",
                "nullable": False,
                "is_primary_key": True,
                "metadata": {"data_type_with_length": "numeric(6)"},
                "column_comment": "태그일련번호",
                "column_description": "태그 마스터 키",
                "ref_schema_name": "rwis_mart",
                "ref_table_name": "vw_tag_dim",
                "ref_column_name": "tagsn",
                "ref_constraint_name": "fk_measure_tag",
                "ref_position": 1,
            }
        )
        self.assertEqual(column.data_type, "numeric(6)")
        self.assertFalse(column.nullable)
        self.assertTrue(column.primary_key)
        self.assertEqual(column.comment, "태그일련번호")
        self.assertEqual(column.description, "태그 마스터 키")
        self.assertIsNotNone(column.references)
        assert column.references is not None
        self.assertEqual(column.references.schema_name, "rwis_mart")
        self.assertEqual(column.references.table_name, "vw_tag_dim")
        self.assertEqual(column.references.column_name, "tagsn")
        self.assertEqual(column.references.constraint_name, "fk_measure_tag")
        self.assertEqual(column.references.position, 1)

    def test_inbound_fk_becomes_referenced_by(self) -> None:
        column = _catalog_column(
            {
                "column_name": "tagsn",
                "dtype": "numeric",
                "inbound_schema_name": "rwis_mart",
                "inbound_table_name": "vw_measure_1min",
                "inbound_column_name": "tagsn",
                "inbound_constraint_name": "fk_measure_tag",
                "inbound_position": 1,
            }
        )
        self.assertIsNotNone(column.referenced_by)
        assert column.referenced_by is not None
        self.assertEqual(len(column.referenced_by), 1)
        self.assertEqual(column.referenced_by[0].table_name, "vw_measure_1min")
        self.assertEqual(column.referenced_by[0].constraint_name, "fk_measure_tag")

    def test_assemble_groups_sources_and_omits_empty_refs(self) -> None:
        catalog = assemble_serving_catalog(
            [
                {
                    "source_name": "rwis_mart_view",
                    "engine": "postgresql",
                    "source_schema": "rwis_mart",
                    "registered_at": "2026-08-20T20:49:27+09:00",
                    "schema_name": "rwis_mart",
                    "table_name": "vw_measure_1min",
                    "table_comment": "1분 계측 원본",
                    "table_description": "1분 계측 fact",
                    "table_metadata": {
                        "logical_name": "1분 계측",
                        "subject_area": "agg",
                    },
                    "column_name": "suj_code",
                    "dtype": "character varying",
                    "nullable": True,
                    "is_primary_key": False,
                    "column_comment": "사업장코드",
                    "column_description": "정수장 코드",
                    "metadata": {"data_type_with_length": "character varying(10)"},
                },
                {
                    "source_name": "rwis_mart_view",
                    "engine": "postgresql",
                    "source_schema": "rwis_mart",
                    "registered_at": "2026-08-20T20:49:27+09:00",
                    "schema_name": "rwis_mart",
                    "table_name": "vw_measure_1min",
                    "column_name": "suj_code",
                    "dtype": "character varying",
                    "nullable": True,
                    "is_primary_key": False,
                    "metadata": {"data_type_with_length": "character varying(10)"},
                    "ref_schema_name": "rwis_mart",
                    "ref_table_name": "vw_tag_dim",
                    "ref_column_name": "suj_code",
                    "inbound_schema_name": "rwis_mart",
                    "inbound_table_name": "vw_tag_dim",
                    "inbound_column_name": "suj_code",
                },
            ],
            serving_active=True,
        )
        self.assertEqual(catalog.serving_status, "active")
        self.assertEqual(len(catalog.sources), 1)
        source = catalog.sources[0]
        self.assertEqual(source.source_name, "rwis_mart_view")
        self.assertEqual(len(source.tables), 1)
        table = source.tables[0]
        self.assertEqual(table.logical_name, "1분 계측")
        self.assertEqual(table.comment, "1분 계측 원본")
        self.assertEqual(table.description, "1분 계측 fact")
        self.assertEqual(table.subject_area, "agg")
        columns = table.columns
        self.assertEqual(len(columns), 1)
        self.assertEqual(columns[0].data_type, "varchar(10)")
        self.assertEqual(columns[0].comment, "사업장코드")
        self.assertEqual(columns[0].description, "정수장 코드")
        self.assertIsNotNone(columns[0].references)
        self.assertIsNotNone(columns[0].referenced_by)
        assert columns[0].referenced_by is not None
        self.assertEqual(columns[0].referenced_by[0].table_name, "vw_tag_dim")

    def test_inactive_when_no_serving_activation(self) -> None:
        catalog = assemble_serving_catalog([], serving_active=False)
        self.assertEqual(catalog.serving_status, "inactive")
        self.assertEqual(catalog.sources, [])

    def test_table_and_column_are_catalog_slices(self) -> None:
        catalog = assemble_serving_catalog(
            [
                {
                    "source_name": "rwis_mart_view",
                    "engine": "postgresql",
                    "source_schema": "rwis_mart",
                    "registered_at": "2026-08-20T20:49:27+09:00",
                    "schema_name": "rwis_mart",
                    "table_name": "vw_measure_1min",
                    "table_metadata": {"logical_name": "1분 계측", "subject_area": "agg"},
                    "column_name": "suj_code",
                    "dtype": "character varying",
                    "nullable": True,
                    "is_primary_key": False,
                    "metadata": {},
                    "ref_schema_name": "rwis_mart",
                    "ref_table_name": "vw_tag_dim",
                    "ref_column_name": "suj_code",
                },
                {
                    "source_name": "rwis_mart_view",
                    "engine": "postgresql",
                    "source_schema": "rwis_mart",
                    "registered_at": "2026-08-20T20:49:27+09:00",
                    "schema_name": "rwis_mart",
                    "table_name": "vw_measure_1min",
                    "column_name": "val",
                    "dtype": "numeric",
                    "nullable": True,
                    "is_primary_key": False,
                    "metadata": {},
                },
                {
                    "source_name": "other_source",
                    "engine": "oracle",
                    "source_schema": "RWIS",
                    "registered_at": "2026-08-20T20:49:27+09:00",
                    "schema_name": "RWIS",
                    "table_name": "RDITAG_TB",
                    "column_name": "TAGSN",
                    "dtype": "NUMBER",
                    "nullable": False,
                    "is_primary_key": True,
                    "metadata": {},
                },
            ],
            serving_active=True,
        )
        table = slice_serving_catalog(
            catalog,
            schema_name="rwis_mart",
            table_name="vw_measure_1min",
        )
        self.assertEqual(len(table.sources), 1)
        self.assertEqual(len(table.sources[0].tables), 1)
        self.assertEqual(len(table.sources[0].tables[0].columns), 2)

        column = slice_serving_catalog(
            catalog,
            source_name="rwis_mart_view",
            schema_name="rwis_mart",
            table_name="vw_measure_1min",
            column_name="VAL",
        )
        self.assertEqual(len(column.sources[0].tables[0].columns), 1)
        self.assertEqual(column.sources[0].tables[0].columns[0].column_name, "val")

        refs = refs_from_catalog(
            slice_serving_catalog(
                catalog,
                schema_name="rwis_mart",
                table_name="vw_measure_1min",
                refs_only=True,
            )
        )
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].column_name, "suj_code")
        self.assertEqual(refs[0].ref_table_name, "vw_tag_dim")

        items = batch_items_from_catalog(catalog)
        self.assertEqual(len(items), 2)
        measure = next(item for item in items if item.table_name == "vw_measure_1min")
        self.assertEqual(measure.source_name, "rwis_mart_view")
        self.assertEqual(measure.table_name_kr, "1분 계측")
        self.assertEqual(measure.subject_area, "agg")


if __name__ == "__main__":
    unittest.main()
