"""entity probe registry 단위 테스트."""
from __future__ import annotations

import unittest

from app.services.entity_probe_registry import (
    load_probe_specs,
    probe_specs_to_columns,
    spec_for_column,
)


class EntityProbeRegistryTest(unittest.TestCase):
    def test_load_rwis_seven_tables(self) -> None:
        specs = load_probe_specs("rwis")
        tables = {s.table.upper() for s in specs}
        self.assertEqual(len(specs), 7)
        self.assertIn("RDITAG_TB", tables)
        self.assertIn("RDISAUP_TB", tables)
        self.assertIn("RDIBONBU_TB", tables)
        self.assertIn("RDIBYUN_TB", tables)
        self.assertIn("RDITAGUNIT_TB", tables)
        self.assertIn("RDIKEPCOTAG_TB", tables)
        self.assertIn("RDIKEPCOTYPE_TB", tables)

    def test_label_columns_count(self) -> None:
        specs = load_probe_specs("rwis")
        cols = probe_specs_to_columns(specs)
        # tag_desc+tag_alias(2) + 6 tables with 1 label each = 8
        self.assertEqual(len(cols), 8)

    def test_spec_for_column(self) -> None:
        specs = load_probe_specs("rwis")
        spec = spec_for_column(specs, table_name="RDIBYUN_TB", label_column="BR_NAME")
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.entity_type, "metric")
        self.assertEqual(spec.code_column, "BR_CODE")

    def test_rditag_dual_labels(self) -> None:
        specs = load_probe_specs("rwis")
        tag = next(s for s in specs if s.table.upper() == "RDITAG_TB")
        self.assertEqual(tag.label_columns, ("TAG_DESC", "TAG_ALIAS"))
        self.assertEqual(tag.code_column, "TAGSN")


if __name__ == "__main__":
    unittest.main()
