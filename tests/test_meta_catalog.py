from __future__ import annotations

import json
import unittest

from app.services.meta_postgres import _batch_item, _column_meta, _table_info


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


if __name__ == "__main__":
    unittest.main()
