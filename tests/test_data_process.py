from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.schemas import PlannedFilter
from app.services.decision_postgres.data_process import (
    bound_suj_names,
    bound_tagsn,
    letter_scope_sql,
    metric_tag_where,
    refine_function,
    replace_tagsn_filter,
    source_uses_process_rules,
    tag_probe_needles,
    tagsn_codes_from_rows,
    usage_keep_rows,
)


class DataProcessUnitTests(unittest.TestCase):
    def test_letter_scope_uses_bound_filters_not_query(self) -> None:
        where = letter_scope_sql(
            [
                PlannedFilter(
                    meaning="사업장",
                    column="rwis_mart.vw_measure_day.suj_code",
                    operator="EQ",
                    value="332",
                    resolution_status="resolved",
                )
            ]
        )
        self.assertEqual(where, "suj_code IN ('332')")
        self.assertIsNone(letter_scope_sql([]))

    def test_bound_tagsn_from_filter(self) -> None:
        tags = bound_tagsn(
            [
                PlannedFilter(
                    meaning="태그",
                    column="rwis_mart.vw_tag_dim.tagsn",
                    operator="EQ",
                    value="136",
                    resolution_status="resolved",
                )
            ]
        )
        self.assertEqual(tags, ["136"])

    def test_bound_suj_skips_like(self) -> None:
        names = bound_suj_names(
            [
                PlannedFilter(
                    meaning="사업장",
                    column="suj_name",
                    operator="LIKE",
                    value="%구천%",
                    resolution_status="resolved",
                )
            ]
        )
        self.assertEqual(names, [])

    def test_mart_logical_name_enables_rules(self) -> None:
        self.assertTrue(
            source_uses_process_rules([{"logical_name": "vw_tag_dim", "schema_name": "rwis_mart"}])
        )
        self.assertFalse(source_uses_process_rules([{"logical_name": "other", "schema_name": "x"}]))

    def test_refine_mixed_sum(self) -> None:
        function, reason = refine_function(
            asked="SUM",
            procedure="aggregate",
            rows=[{"letter": "A"}, {"letter": "M"}],
            grain="day",
        )
        self.assertEqual(function, "NO_SQL")
        self.assertIn("섞여", reason or "")

    def test_refine_hour_day_total_sum(self) -> None:
        function, _reason = refine_function(
            asked="SUM",
            procedure="aggregate",
            rows=[{"letter": "9", "unit_desc": "㎥"}],
            grain="hour",
        )
        self.assertEqual(function, "NO_SQL")

    def test_refine_f_day_identity(self) -> None:
        function, reason = refine_function(
            asked="SUM",
            procedure="aggregate",
            rows=[{"letter": "F", "unit_desc": "㎥/h"}],
            grain="day",
        )
        self.assertEqual(function, "IDENTITY")
        self.assertEqual(reason, "day_total_as_stored")

    def test_refine_gauge_usage_lookup_is_delta(self) -> None:
        function, reason = refine_function(
            asked="IDENTITY",
            procedure="lookup",
            rows=[{"letter": "A", "unit_desc": "㎥"}],
            grain="day",
            query="어제 사용량",
        )
        self.assertEqual(function, "DELTA")
        self.assertEqual(reason, "period_end_minus_prev")

    def test_probe_needles_drop_process_words(self) -> None:
        self.assertEqual(tag_probe_needles(["원수유량", "적산", "136", "사용량"]), ["원수유량"])

    def test_metric_tag_where_needs_location_and_item(self) -> None:
        where = metric_tag_where(
            [
                PlannedFilter(
                    meaning="사업장",
                    column="rwis_mart.vw_measure_day.suj_code",
                    operator="EQ",
                    value="332",
                    resolution_status="resolved",
                )
            ],
            ["원수유량"],
        )
        self.assertIn("suj_code IN ('332')", where or "")
        self.assertIn("원수유량", where or "")
        self.assertIn("원수 유량", where or "")
        self.assertNotIn("적산", where or "")
        self.assertIsNone(metric_tag_where([], ["원수유량"]))

    def test_usage_keep_drops_instant(self) -> None:
        kept = usage_keep_rows(
            [
                {"letter": "Q", "tagsn": "105"},
                {"letter": "A", "tagsn": "136"},
                {"letter": "D", "tagsn": "137"},
            ],
            query="구천 원수유량 어제 사용량",
            asked="SUM",
        )
        self.assertEqual([row["tagsn"] for row in kept], ["136", "137"])

    def test_refine_usage_a_and_d_is_not_one_sum(self) -> None:
        function, combine = refine_function(
            asked="SUM",
            procedure="aggregate",
            rows=[
                {"letter": "A", "tagsn": "136", "unit_desc": "㎥"},
                {"letter": "D", "tagsn": "137", "unit_desc": "㎥"},
            ],
            grain="day",
            query="어제 사용량",
        )
        self.assertEqual(function, "USAGE")
        self.assertIn("136:A:DELTA", combine or "")
        self.assertIn("137:D:IDENTITY", combine or "")

    def test_tagsn_codes_normalize_float(self) -> None:
        self.assertEqual(
            tagsn_codes_from_rows([{"tagsn": "136.0"}, {"tagsn": "137"}]),
            ["136", "137"],
        )

    def test_replace_tagsn_filter(self) -> None:
        filters = replace_tagsn_filter(
            [
                PlannedFilter(
                    meaning="사업장",
                    column="rwis_mart.vw_measure_day.suj_code",
                    operator="EQ",
                    value="332",
                    resolution_status="resolved",
                )
            ],
            column="rwis_mart.vw_measure_day.tagsn",
            codes=["136", "137"],
        )
        tagsn = [item for item in filters if str(item.column or "").endswith("tagsn")]
        self.assertEqual(len(tagsn), 1)
        self.assertEqual(tagsn[0].operator, "IN")
        self.assertEqual(tagsn[0].value, "136,137")


if __name__ == "__main__":
    unittest.main()
