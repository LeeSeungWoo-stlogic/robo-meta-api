from __future__ import annotations

import unittest

from app.services.decision_postgres.filters import (
    _propagate_filters_along_fk,
    _with_metric_filter_requirements,
)
from app.services.metadata_repository._search import SearchMixin
from app.schemas import (
    FilterRequirement,
    MeasurementRequirement,
    PlannedFilter,
    QueryAnalysis,
)


class ValueMappingMentionTests(unittest.TestCase):
    def test_space_insensitive_match_for_metric_label(self) -> None:
        self.assertTrue(
            SearchMixin._natural_value_is_standalone_mention(
                "화성정수장 평균 탁도",
                "탁 도",
            )
        )

    def test_plant_label_before_metric_is_standalone(self) -> None:
        self.assertTrue(
            SearchMixin._natural_value_is_standalone_mention(
                "화성정수장 평균 탁도",
                "화성정수장",
            )
        )

    def test_rejects_mid_word_hangul(self) -> None:
        self.assertFalse(
            SearchMixin._natural_value_is_standalone_mention(
                "정수장 목록",
                "정수",
            )
        )


class MetricFilterRequirementTests(unittest.TestCase):
    def test_appends_metric_filter_when_mapping_exists(self) -> None:
        analysis = QueryAnalysis(
            status="complete",
            measurement=MeasurementRequirement(metric="탁도", aggregation="AVG"),
            filter_requirements=[
                FilterRequirement(meaning="정수장 명칭", value_text="화성정수장"),
            ],
        )
        mappings = [{"natural_value": "탁 도", "code_value": "TB"}]
        requirements = _with_metric_filter_requirements(analysis, mappings)
        self.assertEqual(len(requirements), 2)
        self.assertEqual(requirements[-1].value_text, "탁도")
        self.assertTrue(requirements[-1].meaning.startswith("측정항목:"))

    def test_skips_when_no_mapping(self) -> None:
        analysis = QueryAnalysis(
            status="complete",
            measurement=MeasurementRequirement(metric="연결 오류"),
            filter_requirements=[],
        )
        self.assertEqual(
            len(_with_metric_filter_requirements(analysis, mappings=[])),
            0,
        )

    def test_skips_when_metric_already_present(self) -> None:
        analysis = QueryAnalysis(
            status="complete",
            measurement=MeasurementRequirement(metric="탁도"),
            filter_requirements=[
                FilterRequirement(meaning="측정항목", value_text="탁도"),
            ],
        )
        mappings = [{"natural_value": "탁 도", "code_value": "TB"}]
        self.assertEqual(len(_with_metric_filter_requirements(analysis, mappings)), 1)


class FilterPropagationTests(unittest.TestCase):
    def test_propagates_resolved_eq_across_approved_fk(self) -> None:
        planned = [
            PlannedFilter(
                meaning="측정항목:탁도",
                column="RWIS.RDIBYUN_TB.BR_CODE",
                operator="EQ",
                value="TB",
                resolution_status="resolved",
                confidence=1.0,
            ),
            PlannedFilter(
                meaning="정수장 명칭",
                column="RWIS.RDISAUP_TB.SUJ_CODE",
                operator="EQ",
                value="617",
                resolution_status="resolved",
                confidence=1.0,
            ),
        ]
        edges = [
            {
                "from_table_id": 3,
                "from_schema": "RWIS",
                "from_table": "RDITAG_TB",
                "from_column": "BR_CODE",
                "to_table_id": 4,
                "to_schema": "RWIS",
                "to_table": "RDIBYUN_TB",
                "to_column": "BR_CODE",
            },
            {
                "from_table_id": 3,
                "from_schema": "RWIS",
                "from_table": "RDITAG_TB",
                "from_column": "SUJ_CODE",
                "to_table_id": 1,
                "to_schema": "RWIS",
                "to_table": "RDISAUP_TB",
                "to_column": "SUJ_CODE",
            },
        ]
        tables_by_id = {
            1: {"schema_name": "RWIS", "original_name": "RDISAUP_TB"},
            3: {"schema_name": "RWIS", "original_name": "RDITAG_TB"},
            4: {"schema_name": "RWIS", "original_name": "RDIBYUN_TB"},
        }
        out = _propagate_filters_along_fk(
            planned,
            edges,
            anchor_table_ids={1, 3},
            tables_by_id=tables_by_id,
        )
        columns = {item.column for item in out}
        self.assertIn("RWIS.RDITAG_TB.BR_CODE", columns)
        self.assertIn("RWIS.RDITAG_TB.SUJ_CODE", columns)
        by_column = {item.column: item.value for item in out}
        self.assertEqual(by_column["RWIS.RDITAG_TB.BR_CODE"], "TB")
        self.assertEqual(by_column["RWIS.RDITAG_TB.SUJ_CODE"], "617")

    def test_does_not_invent_endpoints_outside_anchor_set(self) -> None:
        planned = [
            PlannedFilter(
                meaning="측정항목:탁도",
                column="RWIS.RDIBYUN_TB.BR_CODE",
                operator="EQ",
                value="TB",
                resolution_status="resolved",
                confidence=1.0,
            ),
        ]
        edges = [
            {
                "from_table_id": 3,
                "from_schema": "RWIS",
                "from_table": "RDITAG_TB",
                "from_column": "BR_CODE",
                "to_table_id": 4,
                "to_schema": "RWIS",
                "to_table": "RDIBYUN_TB",
                "to_column": "BR_CODE",
            },
        ]
        out = _propagate_filters_along_fk(
            planned,
            edges,
            anchor_table_ids=set(),
            tables_by_id={},
        )
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
