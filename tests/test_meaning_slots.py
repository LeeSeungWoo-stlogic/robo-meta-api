from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.schemas import QueryAnalysis, SchemaRoleRequirement
from app.services.meaning_slots import (
    catalog_needles_from_analysis,
    extremum_function_from_text,
    filter_needles_from_analysis,
    metric_needles_from_analysis,
    range_needles_from_analysis,
    range_slots_from_analysis,
)


class CatalogNeedlesTests(unittest.TestCase):
    def test_goal_and_definitions_are_not_needles(self) -> None:
        analysis = QueryAnalysis(
            status="complete",
            meaning_status="complete",
            goal="금강권역 정수장 목록을 확보한다",
            procedure="list",
            target="금강권역 정수장",
            primary_outputs=["정수장 목록"],
            meaning_roles=[
                SchemaRoleRequirement(
                    role="시설 유형",
                    necessity="required",
                    cardinality="many",
                    search_terms=[
                        "정수장",
                        "물을 정수하여 공급하는 시설",
                        "금강 유역에 해당하는 권역",
                    ],
                )
            ],
        )
        catalog = catalog_needles_from_analysis(analysis, "금강권역 정수장 목록")
        filters = filter_needles_from_analysis(analysis)
        compact_catalog = {item.replace(" ", "") for item in catalog}
        self.assertIn("정수장", compact_catalog)
        self.assertIn("금강권역", compact_catalog)
        self.assertNotIn("금강권역정수장목록을확보한다", compact_catalog)
        self.assertNotIn("물을정수하여공급하는시설", compact_catalog)
        self.assertNotIn("금강유역에해당하는권역", compact_catalog)
        self.assertNotIn("시설유형", compact_catalog)
        self.assertEqual(
            {item.replace(" ", "") for item in filters},
            {"금강권역"},
        )
        self.assertEqual(
            {item.replace(" ", "") for item in range_needles_from_analysis(analysis)},
            {"금강권역"},
        )
        self.assertEqual(metric_needles_from_analysis(analysis), [])

    def test_metric_is_kept_when_it_is_the_answer_axis(self) -> None:
        analysis = QueryAnalysis(
            status="complete",
            meaning_status="complete",
            procedure="aggregate",
            metric="평균 탁도",
            primary_outputs=["평균 탁도"],
            meaning_roles=[
                SchemaRoleRequirement(
                    role="측정항목",
                    necessity="required",
                    cardinality="one",
                    search_terms=["탁도"],
                )
            ],
        )
        found = {
            item.replace(" ", "")
            for item in metric_needles_from_analysis(
                analysis, "2024년 화성정수장 평균 탁도"
            )
        }
        self.assertIn("탁도", found)


class ExtremumAndMetricSurfaceTests(unittest.TestCase):
    def test_low_phrase_is_min(self) -> None:
        self.assertEqual(
            extremum_function_from_text("월 평균 PH가 제일 낮은 곳이 어디야"),
            "MIN",
        )
        self.assertEqual(
            extremum_function_from_text("가장 강우량이 많았던 정수장"),
            "MAX",
        )

    def test_ph_and_acidity_are_metric_needles(self) -> None:
        analysis = QueryAnalysis(
            status="complete",
            meaning_status="complete",
            procedure="extremum",
            metric="pH",
            primary_outputs=["장소", "pH"],
            meaning_roles=[
                SchemaRoleRequirement(
                    role="측정항목",
                    necessity="required",
                    cardinality="one",
                    search_terms=["pH", "산도"],
                )
            ],
        )
        found = {
            item.replace(" ", "").casefold()
            for item in metric_needles_from_analysis(
                analysis,
                "2025년 9월 청주정수장에서 월 평균 PH가 제일 낮은 곳이 어디야?",
            )
        }
        self.assertIn("ph", found)
        self.assertIn("산도", found)


class RangeSlotSplitTests(unittest.TestCase):
    def test_exclude_slot_is_not_eq_needle(self) -> None:
        analysis = QueryAnalysis(
            status="complete",
            meaning_status="complete",
            procedure="list",
            target="금강권역이 아닌 정수장",
            primary_outputs=["정수장 목록"],
        )
        slots = range_slots_from_analysis(analysis, "금강권역이 아닌 정수장")
        self.assertEqual(
            [(item.mention.replace(" ", ""), item.polarity) for item in slots],
            [("금강권역", "exclude")],
        )
        self.assertEqual(range_needles_from_analysis(analysis, "금강권역이 아닌 정수장"), [])

    def test_or_is_two_include_slots(self) -> None:
        analysis = QueryAnalysis(
            status="complete",
            meaning_status="complete",
            procedure="list",
            target="금강권역 또는 낙동강권역 정수장",
            primary_outputs=["정수장 목록"],
        )
        slots = range_slots_from_analysis(
            analysis, "금강권역 또는 낙동강권역 정수장 목록"
        )
        mentions = {item.mention.replace(" ", "") for item in slots}
        self.assertEqual(mentions, {"금강권역", "낙동강권역"})
        self.assertTrue(all(item.polarity == "include" for item in slots))
        self.assertEqual(len(slots), 2)

    def test_period_is_stripped_from_range_remainder(self) -> None:
        analysis = QueryAnalysis(
            status="complete",
            meaning_status="complete",
            procedure="aggregate",
            target="화성정수장",
            metric="탁도",
            period="2024년",
            primary_outputs=["평균 탁도"],
        )
        slots = range_slots_from_analysis(
            analysis, "2024년 화성정수장 평균 탁도"
        )
        mentions = {item.mention.replace(" ", "") for item in slots}
        self.assertEqual(mentions, {"화성정수장"})
        self.assertNotIn("2024년화성정수장", mentions)

    def test_except_tail_does_not_add_include_slot(self) -> None:
        analysis = QueryAnalysis(
            status="complete",
            meaning_status="complete",
            procedure="list",
            target="금강권역 외",
            primary_outputs=["정수장 목록"],
        )
        slots = range_slots_from_analysis(
            analysis, "금강권역이 아닌 정수장 목록"
        )
        self.assertEqual(
            [(item.mention.replace(" ", ""), item.polarity) for item in slots],
            [("금강권역", "exclude")],
        )


if __name__ == "__main__":
    unittest.main()
