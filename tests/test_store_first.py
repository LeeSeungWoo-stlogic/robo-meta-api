from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.schemas import (
    CandidateEvidence,
    MeasurementRequirement,
    QueryAnalysis,
    SchemaRoleRequirement,
)
from app.services.decision_postgres.candidate_evidence import (
    build_candidate_evidence,
)
from app.services.decision_postgres.decide import decide
from app.services.decision_postgres.fact_choose import (
    reset_fact_chooser,
    set_fact_chooser,
)
from app.services.decision_postgres.period import parse_korean_period
from app.services.decision_planner import build_composite_edges
from app.services.decision_postgres.select_store import apply_store_selection
from app.services.decision_postgres.store_first import (
    PERIOD_REQUIRED,
    aggregation_contract,
    dimension_identity_column_names,
    fact_time_column_names,
    filter_mappings_to_labels,
    is_category_mention,
    is_groupby_mention,
    is_list_target_mention,
    is_tag_master_table,
    list_axis_identity_column_names,
    list_axis_skips_tag_identity,
    mapping_filters,
    measure_column_names,
    measure_point_label_filters,
    measurement_needs_period,
    partition_mention_mappings,
    period_filter_for_fact,
    pick_fact_tables,
    prefer_day_grain_facts,
    project_code_mappings_to_hub,
    promote_series_identity_tables,
    query_requests_fact,
    resolve_time_role,
    rewrite_mapping_filters_onto_facts,
    approved_code_mappings,
)
from app.services.metadata_repository._search import SearchMixin


def _runtime() -> MagicMock:
    runtime = MagicMock()
    runtime.decision.table_top_k = 10
    runtime.decision.fk_max_hops = 3
    return runtime


def _fk_edge(
    from_id: int,
    to_id: int,
    from_table: str,
    to_table: str,
    from_col: str,
    to_col: str,
) -> dict:
    return {
        "from_table_id": from_id,
        "from_schema": "S",
        "from_table": from_table,
        "from_column": from_col,
        "to_table_id": to_id,
        "to_schema": "S",
        "to_table": to_table,
        "to_column": to_col,
        "constraint_name": "fk",
        "metadata": {},
    }


def _parent_child_tables() -> dict[int, dict]:
    return {
        1: {
            "id": 1,
            "logical_name": "일 DATA",
            "subject_area": "agg",
            "original_name": "FACT_DD",
        },
        2: {
            "id": 2,
            "logical_name": "태그 마스터",
            "subject_area": "master",
            "original_name": "TAG_TB",
        },
        10: {
            "id": 10,
            "logical_name": "별량",
            "subject_area": "code",
            "original_name": "PARENT_TB",
        },
        20: {
            "id": 20,
            "logical_name": "변량기능",
            "subject_area": "code",
            "original_name": "CHILD_TB",
        },
    }


def _parent_child_edges() -> list[dict]:
    return [
        _fk_edge(1, 2, "FACT_DD", "TAG_TB", "TAGSN", "TAGSN"),
        _fk_edge(20, 10, "CHILD_TB", "PARENT_TB", "PARENT_CODE", "PARENT_CODE"),
    ]


def _parent_child_columns(*, hub_codes: tuple[str, ...]) -> dict[int, list[dict]]:
    hub = [{"name": "TAGSN"}]
    hub.extend({"name": name} for name in hub_codes)
    return {
        1: [{"name": "TAGSN"}, {"name": "VAL"}],
        2: hub,
        10: [{"name": "PARENT_CODE"}],
        20: [{"name": "CHILD_CODE"}, {"name": "PARENT_CODE"}],
    }


def _parent_child_code_rows() -> list[dict]:
    return [
        {
            "natural_value": "항목",
            "code_value": "P",
            "matched_mention": "항목",
            "table_id": 10,
            "column_name": "PARENT_CODE",
            "column_fqn": "S.PARENT_TB.PARENT_CODE",
        },
        {
            "natural_value": "항목 적산",
            "code_value": "C",
            "matched_mention": "항목",
            "table_id": 20,
            "column_name": "CHILD_CODE",
            "column_fqn": "S.CHILD_TB.CHILD_CODE",
        },
    ]


class StoreFirstUnitTests(unittest.TestCase):
    def test_label_starts_with_prefix_not_mid(self) -> None:
        self.assertTrue(SearchMixin._label_starts_with("AAABB정수", "AAA"))
        self.assertFalse(SearchMixin._label_starts_with("XXAAABB", "AAA"))

    def test_unmatched_token_emits_prefixes(self) -> None:
        prefixes = SearchMixin.prefixes_for_unmatched(["AAABB"], [])
        self.assertIn("aaab", prefixes)
        self.assertIn("aaa", prefixes)
        self.assertEqual(SearchMixin.prefixes_for_unmatched(["AAABB"], ["AAABB장"]), [])

    def test_filter_mappings_keeps_chosen_label(self) -> None:
        rows = [
            {"natural_value": "AAABB장", "code_value": "1"},
            {"natural_value": "AAACC장", "code_value": "2"},
        ]
        kept = filter_mappings_to_labels(rows, ["AAABB장"])
        self.assertEqual([row["code_value"] for row in kept], ["1"])
        self.assertEqual(filter_mappings_to_labels(rows, ["ZZZ"]), [])

    def test_groupby_mention_is_not_a_value_filter(self) -> None:
        self.assertTrue(is_groupby_mention("사업장별로 집계", "사업장"))
        self.assertTrue(is_category_mention("떨어진 사업장들 본부별", "사업장들"))
        self.assertTrue(is_category_mention("떨어진 사업장들 본부별", "사업장"))
        self.assertFalse(is_groupby_mention("청주정수장 PH", "사업장"))

    def test_count_column_is_not_weight_without_store_weight_term(self) -> None:
        fact = {"id": 1, "original_name": "FACT_DD", "schema_name": "S"}
        columns = {
            1: [
                {
                    "name": "VAL",
                    "dtype": "numeric",
                    "metadata": {"column_name_kr": "측정값"},
                },
                {
                    "name": "CNT",
                    "dtype": "int",
                    "metadata": {"column_name_kr": "기록 건수"},
                },
            ]
        }
        analysis = QueryAnalysis(
            status="complete",
            procedure="aggregate",
            measurement=MeasurementRequirement(aggregation="AVG"),
        )
        period = parse_korean_period("2024년")
        spec = aggregation_contract(
            analysis=analysis,
            facts=[fact],
            columns_by_id=columns,
            period=period,
        )
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.function, "AVG")
        self.assertEqual(spec.value_column, "VAL")
        self.assertEqual(spec.weighted, False)
        self.assertIsNone(spec.weight_column)
        self.assertEqual(spec.time_scope, "2024-01-01/2024-12-31")

    def test_aggregation_contract_gauge_sum_becomes_delta(self) -> None:
        spec = aggregation_contract(
            analysis=QueryAnalysis(
                status="complete",
                procedure="aggregate",
                measurement=MeasurementRequirement(aggregation="SUM"),
            ),
            facts=[{"id": 1, "original_name": "FACT_DD", "schema_name": "S"}],
            columns_by_id={
                1: [
                    {
                        "name": "VAL",
                        "dtype": "numeric",
                        "metadata": {"column_name_kr": "측정값"},
                    }
                ]
            },
            period=parse_korean_period("어제"),
            process_rows=[{"letter": "A", "unit_desc": "㎥"}],
            grain="day",
        )
        assert spec is not None
        self.assertEqual(spec.function, "DELTA")
        self.assertEqual(spec.tag_combine, "period_end_minus_prev")

    def test_aggregation_contract_instant_sum_is_no_sql(self) -> None:
        spec = aggregation_contract(
            analysis=QueryAnalysis(
                status="complete",
                procedure="aggregate",
                measurement=MeasurementRequirement(aggregation="SUM"),
            ),
            facts=[{"id": 1, "original_name": "FACT_DD", "schema_name": "S"}],
            columns_by_id={
                1: [
                    {
                        "name": "VAL",
                        "dtype": "numeric",
                        "metadata": {"column_name_kr": "측정값"},
                    }
                ]
            },
            period=None,
            query="어제 합계",
            process_rows=[{"letter": "M", "unit_desc": "%"}],
            grain="day",
        )
        assert spec is not None
        self.assertEqual(spec.function, "NO_SQL")

    def test_aggregation_contract_list_stays_empty(self) -> None:
        spec = aggregation_contract(
            analysis=QueryAnalysis(status="complete", procedure="list"),
            facts=[],
            columns_by_id={},
            period=None,
            query="금강유역본부 사업장 목록",
            process_rows=[{"letter": "M", "unit_desc": "%"}],
        )
        self.assertIsNone(spec)

    def test_measure_column_names_skips_identity_and_join_keys(self) -> None:
        names = measure_column_names(
            [
                {
                    "name": "TAGSN",
                    "dtype": "int",
                    "is_foreign_key": True,
                    "metadata": {"column_name_kr": "태그 식별값"},
                },
                {
                    "name": "SUJ_CODE",
                    "dtype": "int",
                    "metadata": {"column_name_kr": "사업장코드"},
                },
                {
                    "name": "VAL",
                    "dtype": "numeric",
                    "metadata": {"column_name_kr": "측정값"},
                },
                {
                    "name": "CNT",
                    "dtype": "int",
                    "metadata": {"column_name_kr": "기록 건수"},
                },
            ],
            exclude={"TAGSN"},
        )
        self.assertEqual(names, ["VAL", "CNT"])

    def test_aggregation_contract_skips_series_identity(self) -> None:
        spec = aggregation_contract(
            analysis=QueryAnalysis(
                status="complete",
                procedure="aggregate",
                measurement=MeasurementRequirement(aggregation="AVG"),
            ),
            facts=[{"id": 1, "original_name": "FACT_DD", "schema_name": "S"}],
            columns_by_id={
                1: [
                    {
                        "name": "TAGSN",
                        "dtype": "int",
                        "is_foreign_key": True,
                        "metadata": {"column_name_kr": "태그 식별값"},
                    },
                    {
                        "name": "VAL",
                        "dtype": "numeric",
                        "metadata": {"column_name_kr": "측정값"},
                    },
                    {
                        "name": "CNT",
                        "dtype": "int",
                        "metadata": {"column_name_kr": "기록 건수"},
                    },
                ]
            },
            period=parse_korean_period("2024년"),
        )
        assert spec is not None
        self.assertEqual(spec.value_column, "VAL")

    def test_countish_numeric_loses_to_measured_value(self) -> None:
        fact = {"id": 1, "original_name": "FACT_DD", "schema_name": "S"}
        spec = aggregation_contract(
            analysis=QueryAnalysis(
                status="complete",
                procedure="aggregate",
                measurement=MeasurementRequirement(aggregation="AVG"),
            ),
            facts=[fact],
            columns_by_id={
                1: [
                    {
                        "name": "CNT",
                        "dtype": "numeric",
                        "metadata": {"column_name_kr": "기록 건수"},
                    },
                    {
                        "name": "VAL",
                        "dtype": "numeric",
                        "metadata": {"column_name_kr": "측정값"},
                    },
                ]
            },
            period=parse_korean_period("2024년"),
        )
        assert spec is not None
        self.assertEqual(spec.value_column, "VAL")
        self.assertEqual(spec.weighted, False)

    def test_weight_column_only_when_meta_says_weight(self) -> None:
        fact = {"id": 1, "original_name": "FACT_DD", "schema_name": "S"}
        columns = {
            1: [
                {
                    "name": "VAL",
                    "dtype": "float",
                    "metadata": {"column_name_kr": "측정값"},
                },
                {
                    "name": "WGT",
                    "dtype": "int",
                    "metadata": {"column_name_kr": "가중 건수"},
                },
            ]
        }
        spec = aggregation_contract(
            analysis=QueryAnalysis(
                status="complete",
                procedure="aggregate",
                measurement=MeasurementRequirement(),
            ),
            facts=[fact],
            columns_by_id=columns,
            period=parse_korean_period("2024년"),
        )
        assert spec is not None
        self.assertTrue(spec.weighted)
        self.assertEqual(spec.weight_column, "WGT")
        rows = [
            {
                "column_fqn": "S.DIM.SUJ_CODE",
                "code_value": "981",
                "natural_value": "임시1",
                "matched_mention": "사업장",
            },
            {
                "column_fqn": "S.DIM.SUJ_CODE",
                "code_value": "997",
                "natural_value": "임시2",
                "matched_mention": "사업장",
            },
            {
                "column_fqn": "S.DIM.BONBU_CODE",
                "code_value": "330",
                "natural_value": "한강지역본부",
                "matched_mention": "한강",
            },
        ]
        filters = mapping_filters(rows, query="한강 사업장별 집계")
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0].value, "330")
        self.assertEqual(filters[0].operator, "EQ")
        type_rows = [
            {
                "column_fqn": "S.DIM.SUJ_CODE",
                "code_value": "981",
                "natural_value": "사업장 임시 코드",
                "matched_mention": "사업장",
                "logical_name": "사업장",
            },
            {
                "column_fqn": "S.DIM.SUJ_CODE",
                "code_value": "997",
                "natural_value": "사업장 임시 코드",
                "matched_mention": "사업장",
                "logical_name": "사업장",
            },
        ]
        self.assertEqual(mapping_filters(type_rows, query="잔류염소 사업장 집계"), [])
        hq_and_plant = [
            {
                "column_fqn": "S.HQ.BNB_CODE",
                "code_value": "701",
                "natural_value": "금강유역본부(충청)",
                "matched_mention": "금강권역",
                "logical_name": "지역본부",
            },
            {
                "column_fqn": "S.PLANT.SUJ_CODE",
                "code_value": "701",
                "natural_value": "금강남부권지역본부",
                "matched_mention": "금강",
                "logical_name": "사업장",
            },
        ]
        hq_filters = mapping_filters(hq_and_plant, query="금강권역 정수장 목록")
        self.assertEqual(len(hq_filters), 1)
        self.assertEqual(hq_filters[0].column, "S.HQ.BNB_CODE")

    def test_coordinated_list_targets_are_not_code_filters(self) -> None:
        query = "한강유역 본부에 가장 강우량이 많았던 정수장이나 설비 알려주세요"
        self.assertTrue(is_list_target_mention(query, "설비"))
        self.assertTrue(is_list_target_mention(query, "정수장"))
        self.assertFalse(is_list_target_mention(query, "강우량"))
        self.assertFalse(is_list_target_mention("설비온도가 높았던 곳 알려주세요", "설비"))
        rows = [
            {
                "column_fqn": "S.BYUN.BR_CODE",
                "code_value": "RA",
                "natural_value": "강우량(Rainfall)",
                "matched_mention": "강우량",
                "logical_name": "별량코드 정보",
            },
            {
                "column_fqn": "S.BYUN.BR_CODE",
                "code_value": "FT",
                "natural_value": "설비온도",
                "matched_mention": "설비",
                "logical_name": "별량코드 정보",
            },
        ]
        filters = mapping_filters(rows, query=query)
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0].operator, "EQ")
        needles = measure_point_label_filters(
            query,
            rows,
            tables_by_id={
                2: {
                    "id": 2,
                    "logical_name": "태그 마스터",
                    "subject_area": "master",
                    "schema_name": "RWIS",
                    "original_name": "rditag_tb",
                }
            },
            columns_by_id={
                2: [
                    {
                        "name": "TAG_ADD_DC",
                        "metadata": {"column_name_kr": "태그 설명"},
                    }
                ]
            },
            table_ids={2},
        )
        self.assertEqual([item.value for item in needles], [])

    def test_or_range_instances_are_not_list_targets(self) -> None:
        query = "금강권역 또는 낙동강권역 정수장 목록"
        self.assertFalse(is_list_target_mention(query, "금강권역"))
        self.assertFalse(is_list_target_mention(query, "낙동강권역"))
        self.assertTrue(is_list_target_mention(query, "정수장"))
        rows = [
            {
                "column_fqn": "S.HQ.BNB_CODE",
                "code_value": "902",
                "natural_value": "금강유역본부",
                "matched_mention": "금강권역",
                "logical_name": "지역본부",
            },
            {
                "column_fqn": "S.HQ.BNB_CODE",
                "code_value": "701",
                "natural_value": "금강유역본부(충청)",
                "matched_mention": "금강권역",
                "logical_name": "지역본부",
            },
            {
                "column_fqn": "S.HQ.BNB_CODE",
                "code_value": "802",
                "natural_value": "낙동강유역본부",
                "matched_mention": "낙동강권역",
                "logical_name": "지역본부",
            },
            {
                "column_fqn": "S.HQ.BNB_CODE",
                "code_value": "801",
                "natural_value": "낙동강유역본부(경남)",
                "matched_mention": "낙동강권역",
                "logical_name": "지역본부",
            },
        ]
        filters = mapping_filters(rows, query=query)
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0].operator, "IN")
        self.assertEqual(
            {item.strip() for item in str(filters[0].value).split(",")},
            {"701", "801", "802", "902"},
        )

    def test_list_query_does_not_request_fact(self) -> None:
        listed = QueryAnalysis(
            status="complete",
            procedure="list",
            meaning_status="complete",
            primary_outputs=["정수장"],
        )
        self.assertFalse(query_requests_fact("금강권역 정수장 목록", None, listed))
        self.assertFalse(
            query_requests_fact(
                "아산정수장에서 측정되고 있는 데이터들은 어떤 게 있어?",
                None,
                listed,
            )
        )
        metric = QueryAnalysis(
            status="complete",
            procedure="aggregate",
            metric="잔류염소",
            meaning_status="complete",
        )
        period = parse_korean_period("25년 10월 잔류염소")
        self.assertTrue(query_requests_fact("25년 10월 잔류염소", period, metric))
        self.assertFalse(
            query_requests_fact("2025년 10월 사업장 목록", None, listed)
        )
        gapped = QueryAnalysis(
            status="complete",
            procedure="extremum",
            metric="강우량",
            meaning_status="complete",
            primary_outputs=["정수장", "설비"],
        )
        self.assertTrue(
            query_requests_fact(
                "한강유역 본부에 가장 강우량이 많았던 정수장이나 설비 알려주세요",
                None,
                gapped,
            )
        )
        self.assertEqual(resolve_time_role(procedure="extremum"), "extremum")
        self.assertEqual(resolve_time_role(procedure="list"), "none")
        lookup_metric = QueryAnalysis(
            status="complete",
            procedure="lookup",
            metric="공급량",
            meaning_status="complete",
        )
        year = parse_korean_period("2024년")
        self.assertTrue(
            query_requests_fact("충주 2024년 공급량은?", year, lookup_metric)
        )
        self.assertTrue(
            measurement_needs_period("충주 취수 공급량은?", lookup_metric)
        )
        self.assertFalse(measurement_needs_period("금강권역 정수장 목록", listed))
        self.assertFalse(
            measurement_needs_period("충주정수장 2024년 평균 탁도", lookup_metric)
        )

    def test_series_or_period_metric_list_requests_fact(self) -> None:
        series = QueryAnalysis(
            status="complete",
            procedure="list",
            metric="탁도",
            meaning_status="complete",
            primary_outputs=["측정시점", "탁도"],
            schema_roles=[
                SchemaRoleRequirement(
                    role="측정항목",
                    necessity="required",
                    cardinality="one",
                    search_terms=["탁도"],
                )
            ],
        )
        period = parse_korean_period("충주정수장 2025년 8월 탁도변화 알려줘")
        self.assertTrue(
            query_requests_fact(
                "충주정수장 2025년 8월 탁도변화 알려줘",
                period,
                series,
            )
        )
        rows = [
            {
                "column_fqn": "S.BYUN.BR_CODE",
                "code_value": "TB",
                "natural_value": "탁도",
                "matched_mention": "탁도",
                "logical_name": "별량코드 정보",
            }
        ]
        filters = mapping_filters(rows, query="충주정수장 2025년 8월 탁도변화 알려줘", analysis=series)
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0].value, "TB")

    def test_extremum_low_overrides_max_default(self) -> None:
        fact = {"id": 1, "original_name": "FACT_MM", "schema_name": "S"}
        spec = aggregation_contract(
            analysis=QueryAnalysis(
                status="complete",
                procedure="extremum",
                measurement=MeasurementRequirement(aggregation="MAX"),
                metric="pH",
            ),
            facts=[fact],
            columns_by_id={
                1: [
                    {
                        "name": "VAL",
                        "dtype": "numeric",
                        "metadata": {"column_name_kr": "측정값"},
                    }
                ]
            },
            period=parse_korean_period("2025년 9월"),
            query="2025년 9월 청주정수장에서 월 평균 PH가 제일 낮은 곳이 어디야?",
        )
        assert spec is not None
        self.assertEqual(spec.function, "MIN")

    def test_unspecified_grain_defaults_to_day_fact(self) -> None:
        tables = [
            {
                "id": 1,
                "logical_name": "월 DATA",
                "subject_area": "agg",
                "description": "01mm",
            },
            {
                "id": 2,
                "logical_name": "일 DATA",
                "subject_area": "agg",
                "description": "01dd",
            },
            {
                "id": 3,
                "logical_name": "시간 DATA",
                "subject_area": "agg",
                "description": "01hh",
            },
        ]
        picked, err = pick_fact_tables(tables, None, query="가장 강우량이 많았던 곳")
        self.assertIsNone(err)
        self.assertEqual([int(item["id"]) for item in picked], [2])
        self.assertEqual(
            [int(item["id"]) for item in prefer_day_grain_facts(tables)],
            [2],
        )
        month_locked, month_err = pick_fact_tables(
            tables, "month", query="월별 평균"
        )
        self.assertIsNone(month_err)
        self.assertEqual([int(item["id"]) for item in month_locked], [1])

    def test_ambiguous_mention_labels_are_held_back(self) -> None:
        unique, ambiguous = partition_mention_mappings(
            [
                {
                    "natural_value": "AA 임시1",
                    "code_value": "1",
                    "matched_mention": "aa",
                },
                {
                    "natural_value": "AA 임시2",
                    "code_value": "2",
                    "matched_mention": "aa",
                },
                {
                    "natural_value": "BB장",
                    "code_value": "3",
                    "matched_mention": "bb장",
                },
            ]
        )
        self.assertEqual([row["code_value"] for row in unique], ["3"])
        self.assertEqual(
            sorted(row["code_value"] for row in ambiguous),
            ["1", "2"],
        )

    def test_same_column_alias_codes_stay_unique(self) -> None:
        unique, ambiguous = partition_mention_mappings(
            [
                {
                    "natural_value": "한강유역본부",
                    "code_value": "701",
                    "column_fqn": "RWIS.RDIBONBU_TB.BNB_CODE",
                    "matched_mention": "한강유역",
                },
                {
                    "natural_value": "한강유역본부(강원)",
                    "code_value": "703",
                    "column_fqn": "RWIS.RDIBONBU_TB.BNB_CODE",
                    "matched_mention": "한강유역",
                },
            ]
        )
        self.assertEqual(sorted(row["code_value"] for row in unique), ["701", "703"])
        self.assertEqual(ambiguous, [])

    def test_hub_projects_parent_code_when_child_column_absent(self) -> None:
        admitted = project_code_mappings_to_hub(
            [],
            _parent_child_code_rows(),
            "측정값이 많았던 곳",
            selected_ids={1},
            tables_by_id=_parent_child_tables(),
            columns_by_id=_parent_child_columns(hub_codes=("PARENT_CODE",)),
            edge_rows=_parent_child_edges(),
            edges=build_composite_edges(_parent_child_edges()),
            max_hops=3,
        )
        self.assertEqual([row["code_value"] for row in admitted], ["P"])
        self.assertEqual(admitted[0]["column_name"], "PARENT_CODE")

    def test_hub_projects_child_code_when_hub_has_composite(self) -> None:
        admitted = project_code_mappings_to_hub(
            [],
            _parent_child_code_rows(),
            "측정값이 많았던 곳",
            selected_ids={1},
            tables_by_id=_parent_child_tables(),
            columns_by_id=_parent_child_columns(hub_codes=("CHILD_CODE",)),
            edge_rows=_parent_child_edges(),
            edges=build_composite_edges(_parent_child_edges()),
            max_hops=3,
        )
        self.assertEqual([row["code_value"] for row in admitted], ["C"])
        self.assertEqual(admitted[0]["column_name"], "CHILD_CODE")

    def test_child_specific_label_keeps_child_mapping(self) -> None:
        admitted = project_code_mappings_to_hub(
            [],
            _parent_child_code_rows(),
            "항목 적산이 많았던 곳",
            selected_ids={1},
            tables_by_id=_parent_child_tables(),
            columns_by_id=_parent_child_columns(hub_codes=("PARENT_CODE",)),
            edge_rows=_parent_child_edges(),
            edges=build_composite_edges(_parent_child_edges()),
            max_hops=3,
        )
        self.assertEqual([row["code_value"] for row in admitted], ["C"])

    def test_unrelated_ambiguous_codes_stay_held(self) -> None:
        held = [
            {
                "natural_value": "AA 임시1",
                "code_value": "1",
                "matched_mention": "aa",
                "table_id": 30,
                "column_name": "COL_A",
                "column_fqn": "S.TMP1.COL_A",
            },
            {
                "natural_value": "AA 임시2",
                "code_value": "2",
                "matched_mention": "aa",
                "table_id": 31,
                "column_name": "COL_B",
                "column_fqn": "S.TMP2.COL_B",
            },
        ]
        admitted = project_code_mappings_to_hub(
            [],
            held,
            "AA 조회",
            selected_ids={1},
            tables_by_id=_parent_child_tables(),
            columns_by_id=_parent_child_columns(hub_codes=("PARENT_CODE",)),
            edge_rows=_parent_child_edges(),
            edges=build_composite_edges(_parent_child_edges()),
            max_hops=3,
        )
        self.assertEqual(admitted, [])

    def test_pick_fact_uses_store_logical_text(self) -> None:
        tables = [
            {
                "id": 1,
                "logical_name": "월 DATA",
                "subject_area": "agg",
                "description": "",
            },
            {
                "id": 2,
                "logical_name": "일 DATA",
                "subject_area": "agg",
                "description": "",
            },
        ]
        picked, err = pick_fact_tables(tables, "month")
        self.assertIsNone(err)
        self.assertEqual([int(item["id"]) for item in picked], [1])
        kept, kept_err = pick_fact_tables(tables, None, query="항목 조회")
        self.assertEqual([int(item["id"]) for item in kept], [2])
        self.assertIsNone(kept_err)
        mixed = [
            *tables,
            {
                "id": 3,
                "logical_name": "로그",
                "subject_area": "raw",
                "description": "",
            },
        ]
        facts_only, raw_err = pick_fact_tables(mixed, None, query="항목 조회")
        self.assertEqual([int(item["id"]) for item in facts_only], [2])
        self.assertIsNone(raw_err)
        from_query, query_err = pick_fact_tables(
            tables, None, query="한달 평균 항목"
        )
        self.assertIsNone(query_err)
        self.assertEqual([int(item["id"]) for item in from_query], [1])

    def test_period_uses_store_format_not_physical_name(self) -> None:
        fact = {"schema_name": "S", "original_name": "T"}
        dated = period_filter_for_fact(
            "2025년 9월 항목",
            fact,
            [
                {
                    "name": "LOG_TIME",
                    "dtype": "varchar",
                    "metadata": {"format_pattern": "YYYYMM"},
                }
            ],
        )
        self.assertIsNotNone(dated)
        assert dated is not None
        self.assertEqual(dated.resolution_status, "resolved")
        self.assertTrue(dated.column.endswith("LOG_TIME"))
        self.assertEqual(dated.value, "202509%")

        from_json = period_filter_for_fact(
            "2025년 9월 항목",
            fact,
            [
                {
                    "name": "LOG_TIME",
                    "dtype": "varchar",
                    "metadata": '{"format_pattern": "YYYYMMDDHH"}',
                }
            ],
        )
        self.assertIsNotNone(from_json)
        assert from_json is not None
        self.assertEqual(from_json.resolution_status, "resolved")
        self.assertEqual(from_json.operator, "BETWEEN")
        self.assertEqual(from_json.value, "2025090100,2025093023")

        missing = period_filter_for_fact(
            "2025년 9월 항목",
            fact,
            [{"name": "LOG_TIME", "dtype": "varchar", "metadata": {}}],
        )
        self.assertIsNotNone(missing)
        assert missing is not None
        self.assertEqual(missing.resolution_status, "unresolved")

    def test_dimension_identity_opens_label_and_code(self) -> None:
        names = dimension_identity_column_names(
            [
                {
                    "name": "SUJ_CODE",
                    "metadata": {"column_name_kr": "사업장 단위의 식별값"},
                },
                {
                    "name": "SUJ_NM",
                    "metadata": {"column_name_kr": "사업장 식별에 사용하는 명칭"},
                },
                {
                    "name": "NOTE",
                    "metadata": {"column_name_kr": "비고설명"},
                },
                {
                    "name": "GNJ_CODE",
                    "metadata": {"column_name_kr": "사업장이 속한 광역 단위를 식별하는 코드"},
                },
                {
                    "name": "CRT_DT",
                    "dtype": "timestamp",
                    "metadata": {},
                },
                {
                    "name": "BR_GUBUN",
                    "metadata": {"column_name_kr": "변량 구분 코드"},
                },
                {
                    "name": "USE_YN",
                    "metadata": {"column_name_kr": "사용여부"},
                },
            ]
        )
        self.assertIn("SUJ_CODE", names)
        self.assertIn("SUJ_NM", names)
        self.assertNotIn("NOTE", names)
        self.assertNotIn("GNJ_CODE", names)
        self.assertNotIn("BR_GUBUN", names)
        self.assertNotIn("USE_YN", names)
        self.assertNotIn("CRT_DT", names)

    def test_tag_master_identity_keeps_description_label(self) -> None:
        table = {
            "subject_area": "master",
            "logical_name": "태그 마스터",
            "description": "측정항목 태그",
        }
        self.assertTrue(is_tag_master_table(table))
        self.assertFalse(
            is_tag_master_table(
                {
                    "subject_area": "raw",
                    "logical_name": "일 DATA",
                    "description": "태그 식별자별 측정값",
                }
            )
        )
        names = dimension_identity_column_names(
            [
                {
                    "name": "TAGSN",
                    "is_primary_key": True,
                    "metadata": {"column_name_kr": "태그 식별값"},
                },
                {
                    "name": "TAG_DESC",
                    "metadata": {"column_name_kr": "태그 설명"},
                },
                {
                    "name": "NOTE",
                    "metadata": {"column_name_kr": "비고"},
                },
            ],
            table,
        )
        self.assertIn("TAGSN", names)
        self.assertIn("TAG_DESC", names)
        self.assertNotIn("NOTE", names)

    def test_tag_master_identity_drops_locator_and_parent_codes(self) -> None:
        table = {
            "subject_area": "master",
            "logical_name": "태그 마스터",
        }
        names = dimension_identity_column_names(
            [
                {
                    "name": "TAGSN",
                    "is_primary_key": True,
                    "description": "태그를 구분하는 식별용 번호",
                },
                {
                    "name": "TAG_DESC",
                    "description": "태그가 나타내는 항목의 의미를 기록하는 설명 정보",
                },
                {
                    "name": "TAG_NAME",
                    "description": "설비 태그의 별칭을 기록하는 항목",
                },
                {
                    "name": "TAG_ADDR",
                    "description": "태그를 식별하기 위한 주소 정보",
                },
                {
                    "name": "TAG_PATH",
                    "description": "태그를 식별하는 경로",
                },
                {
                    "name": "CALC_FNUM",
                    "description": "계산식에 적용되는 함수 식별 번호",
                },
                {
                    "name": "SITE_NAME",
                    "description": "태그가 속한 사이트의 명칭",
                },
                {
                    "name": "FCLTY_CODE2",
                    "description": "연계 대상 시설을 식별하기 위한 코드",
                },
                {
                    "name": "REPO_FLAG",
                    "description": "보고서 분류를 식별하는 코드",
                },
                {
                    "name": "BR_CODE",
                    "description": "태그에서 변량을 식별하는 코드",
                },
            ],
            table,
        )
        self.assertEqual(set(names), {"TAGSN", "TAG_DESC"})

    def test_plant_list_axis_drops_tagsn(self) -> None:
        table = {
            "subject_area": "master",
            "logical_name": "태그 마스터",
        }
        columns = [
            {
                "name": "tagsn",
                "is_primary_key": True,
                "metadata": {"column_name_kr": "태그일련번호"},
            },
            {
                "name": "suj_code",
                "metadata": {"column_name_kr": "사업장코드"},
            },
            {
                "name": "suj_name",
                "metadata": {"column_name_kr": "사업장이름"},
            },
            {
                "name": "bnb_code",
                "metadata": {"column_name_kr": "유역본부코드"},
            },
            {
                "name": "tag_desc",
                "metadata": {"column_name_kr": "태그설명"},
            },
        ]
        self.assertTrue(list_axis_skips_tag_identity(["정수장"]))
        names = list_axis_identity_column_names(columns, table, ["정수장"])
        self.assertEqual(set(names), {"suj_code", "suj_name"})
        self.assertNotIn("tagsn", names)
        tag_names = dimension_identity_column_names(columns, table)
        self.assertIn("tagsn", tag_names)

    def test_rewrite_mapping_filters_onto_fact_columns(self) -> None:
        from app.schemas import PlannedFilter

        facts = [
            {
                "id": 2,
                "schema_name": "rwis_mart",
                "original_name": "vw_measure_day",
                "name": "vw_measure_day",
            }
        ]
        planned = [
            PlannedFilter(
                meaning="코드매핑:충주정수장",
                column="rwis_mart.vw_tag_dim.suj_code",
                operator="EQ",
                value="380",
                resolution_status="resolved",
            )
        ]
        mappings = [
            {
                "table_id": 1,
                "column_fqn": "rwis_mart.vw_tag_dim.suj_code",
                "column_name": "suj_code",
            }
        ]
        out, rewritten = rewrite_mapping_filters_onto_facts(
            planned,
            facts=facts,
            fact_columns_by_id={2: [{"name": "suj_code"}, {"name": "br_code"}]},
            mappings=mappings,
        )
        self.assertEqual(rewritten, {1})
        self.assertEqual(out[0].column, "rwis_mart.vw_measure_day.suj_code")
        kept, none = rewrite_mapping_filters_onto_facts(
            planned,
            facts=facts,
            fact_columns_by_id={2: [{"name": "VAL"}]},
            mappings=mappings,
        )
        self.assertEqual(none, set())
        self.assertEqual(kept[0].column, "rwis_mart.vw_tag_dim.suj_code")

    def test_apply_store_selection_skips_rewritten_dim(self) -> None:
        mappings = [{"table_id": 1, "column_fqn": "S.DIM.suj_code"}]
        selected, ids = apply_store_selection(
            {"selected_table_ids": [2], "selected_mapping_keys": []},
            mappings=mappings,
            selected_ids={2},
            rewritten_mapping_ids={1},
        )
        self.assertEqual(selected, mappings)
        self.assertEqual(ids, {2})
        _, with_failed = apply_store_selection(
            {"selected_table_ids": [2], "selected_mapping_keys": []},
            mappings=mappings,
            selected_ids={2},
            rewritten_mapping_ids=set(),
        )
        self.assertEqual(with_failed, {2, 1})

    def test_series_promotes_tag_master_off_bridge(self) -> None:
        tables = {
            3: {
                "subject_area": "master",
                "logical_name": "태그 마스터",
            }
        }
        kept = promote_series_identity_tables(
            {3},
            tables_by_id=tables,
            query="충주정수장 2025년 8월 탁도변화 알려줘",
        )
        self.assertEqual(kept, set())
        still_bridge = promote_series_identity_tables(
            {3},
            tables_by_id=tables,
            query="충주정수장 2025년 8월 평균 탁도 알려줘",
            needs_fact=True,
        )
        self.assertEqual(still_bridge, set())

    def test_aggregate_keeps_measure_code_when_metric_is_answer_axis(self) -> None:
        analysis = QueryAnalysis(
            status="complete",
            meaning_status="complete",
            procedure="aggregate",
            metric="탁도",
            primary_outputs=["평균 탁도"],
        )
        filters = mapping_filters(
            [
                {
                    "column_fqn": "S.BYUN.BR_CODE",
                    "code_value": "TB",
                    "natural_value": "탁 도",
                    "matched_mention": "탁도",
                    "logical_name": "측정항목",
                }
            ],
            query="2024년 화성정수장 평균 탁도",
            analysis=analysis,
        )
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0].value, "TB")

    def test_measure_point_label_intersects_code_and_description(self) -> None:
        table = {
            "id": 3,
            "subject_area": "master",
            "logical_name": "태그 마스터",
            "schema_name": "RWIS",
            "original_name": "POINT_MASTER",
        }
        planned = measure_point_label_filters(
            "충주정수장 2025년 8월 탁도변화 알려줘",
            [
                {
                    "matched_mention": "탁도",
                    "natural_value": "탁 도",
                    "logical_name": "별량코드",
                    "column_fqn": "RWIS.CODE.BR_CODE",
                    "column_name": "BR_CODE",
                    "code_value": "TB",
                },
                {
                    "matched_mention": "충주정수장",
                    "natural_value": "충주정수장",
                    "logical_name": "사업장",
                    "column_fqn": "RWIS.PLANT.SUJ_CODE",
                    "column_name": "SUJ_CODE",
                    "code_value": "380",
                },
            ],
            tables_by_id={3: table},
            columns_by_id={
                3: [
                    {
                        "name": "TAG_DESC",
                        "metadata": {"column_name_kr": "태그 설명"},
                    }
                ]
            },
            table_ids={3},
        )
        likes = [item for item in planned if item.operator == "LIKE"]
        unused = [item for item in planned if item.operator == "NOT_LIKE"]
        self.assertEqual(likes, [])
        self.assertEqual(unused, [])

    def test_fact_time_column_names_keeps_store_date_drops_audit(self) -> None:
        names = fact_time_column_names(
            [
                {
                    "name": "MEAS_TM",
                    "dtype": "varchar",
                    "metadata": {"format_pattern": "YYYYMM"},
                },
                {
                    "name": "OBS_DT",
                    "dtype": "date",
                },
                {
                    "name": "CRT_DT",
                    "dtype": "timestamp",
                },
                {
                    "name": "NOTE",
                    "dtype": "varchar",
                },
            ]
        )
        self.assertEqual(names, ["MEAS_TM", "OBS_DT"])
        self.assertNotIn("CRT_DT", names)

    def test_candidate_evidence_rejected_fact_has_reason(self) -> None:
        items = build_candidate_evidence(
            catalog_tables=[
                {
                    "id": 1,
                    "original_name": "PLANT_TB",
                    "schema_name": "S",
                    "logical_name": "사업장",
                    "matched_mention": "정수장",
                    "match_type": "exact",
                    "matched_field": "logical_name",
                    "score": 1.0,
                }
            ],
            fact_tables=[
                {
                    "id": 2,
                    "original_name": "FACT_A",
                    "schema_name": "S",
                    "logical_name": "월 DATA",
                    "description": "월 집계",
                    "subject_area": "agg",
                },
                {
                    "id": 3,
                    "original_name": "FACT_B",
                    "schema_name": "S",
                    "logical_name": "일 DATA",
                    "description": "일 집계",
                    "subject_area": "agg",
                },
            ],
            selected_ids={1, 3},
            chosen_fact_ids={3},
            query_requests_fact=True,
            grain="day",
        )
        by_name = {item.table_name: item for item in items if item.kind == "fact"}
        self.assertTrue(by_name["FACT_B"].selected)
        self.assertFalse(by_name["FACT_A"].selected)
        self.assertTrue(by_name["FACT_A"].reason.strip())
        self.assertTrue(by_name["FACT_B"].reason.strip())
        plant = next(item for item in items if item.kind == "catalog")
        self.assertTrue(plant.selected)
        self.assertEqual(plant.match_type, "exact")
        self.assertEqual(plant.matched_field, "logical_name")

    def test_list_query_fact_evidence_is_one_summary(self) -> None:
        items = build_candidate_evidence(
            fact_tables=[
                {
                    "id": 2,
                    "original_name": "FACT_A",
                    "schema_name": "S",
                    "logical_name": "월 DATA",
                    "subject_area": "agg",
                },
                {
                    "id": 3,
                    "original_name": "FACT_B",
                    "schema_name": "S",
                    "logical_name": "일 DATA",
                    "subject_area": "agg",
                },
            ],
            query_requests_fact=False,
        )
        facts = [item for item in items if item.kind == "fact"]
        self.assertEqual(len(facts), 1)
        self.assertFalse(facts[0].selected)
        self.assertEqual(facts[0].reason, "목록·비측정 질의라 팩트 2개를 계획에 넣지 않음")
        self.assertIsNone(facts[0].table_name)

    def test_candidate_evidence_rejects_blank_reason(self) -> None:
        with self.assertRaises(Exception):
            CandidateEvidence(
                kind="fact",
                selected=False,
                reason="   ",
            )

    def test_embedding_mapping_is_not_bound(self) -> None:
        rows = [
            {
                "natural_value": "금강유역본부",
                "code_value": "999",
                "column_fqn": "S.HQ.BNB_CODE",
                "match_type": "embedding",
                "score": 0.99,
            },
            {
                "natural_value": "금강유역본부",
                "code_value": "902",
                "column_fqn": "S.HQ.BNB_CODE",
                "match_type": "exact",
                "matched_mention": "금강권역",
            },
        ]
        kept = approved_code_mappings(rows)
        self.assertEqual([row["code_value"] for row in kept], ["902"])
        planned = mapping_filters(rows, query="금강권역 조회")
        self.assertEqual(planned[0].value, "902")


class StoreFirstDecideTests(unittest.IsolatedAsyncioTestCase):
    async def test_decide_empty_seed_returns_no_meta(self) -> None:
        repo = AsyncMock()
        repo.find_glossary_routes = AsyncMock(return_value=[])
        repo.find_value_mappings = AsyncMock(return_value=[])
        repo.find_catalog_by_mentions = AsyncMock(return_value=[])
        analyze = AsyncMock(
            return_value=QueryAnalysis(
                status="complete",
                goal="x",
                procedure="lookup",
                meaning_status="complete",
                measurement=MeasurementRequirement(),
                schema_roles=[],
            )
        )
        with (
            patch(
                "app.services.decision_postgres.decide.get_runtime",
                return_value=_runtime(),
            ),
            patch(
                "app.services.decision_postgres.decide.get_query_analyzer"
            ) as getter,
        ):
            getter.return_value.analyze = analyze
            response = await decide(
                repo,
                query="QQ 항목",
                include_matched_columns=False,
                column_top_m=None,
                auto_resolve_entities=True,
            )
        self.assertIn(
            "맞는 메타데이터가 없다",
            response.query_plan.unresolved_requirements or [],
        )
        analyze.assert_called()

    async def test_decide_calls_analyze_before_store(self) -> None:
        order: list[str] = []

        async def glossary(_query: str):
            order.append("glossary")
            return []

        async def mappings(*_args, **_kwargs):
            order.append("mappings")
            return []

        async def catalog(*_args, **_kwargs):
            order.append("catalog")
            return [
                {
                    "id": 1,
                    "original_name": "T1",
                    "name": "T1",
                    "logical_name": "QQ 항목",
                    "subject_area": "master",
                    "schema_name": "S",
                }
            ]

        repo = AsyncMock()
        repo.find_glossary_routes = glossary
        repo.find_value_mappings = mappings
        repo.find_catalog_by_mentions = catalog
        repo.fk_neighbor_table_ids = AsyncMock(return_value=set())
        repo.fetch_tables_by_ids = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "original_name": "T1",
                    "name": "T1",
                    "logical_name": "QQ 항목",
                    "subject_area": "master",
                    "schema_name": "S",
                }
            ]
        )
        repo.fetch_join_edges = AsyncMock(return_value=[])
        repo.fetch_approved_columns = AsyncMock(return_value={})
        repo.execution_source_scope = AsyncMock(return_value=None)

        async def analyze(question: str, timeout_s=None, store_hits=None):
            order.append("analyze")
            self.assertIsNone(store_hits)
            return QueryAnalysis(
                status="complete",
                goal="x",
                procedure="lookup",
                meaning_status="complete",
                measurement=MeasurementRequirement(),
                schema_roles=[],
            )

        with (
            patch(
                "app.services.decision_postgres.decide.get_runtime",
                return_value=_runtime(),
            ),
            patch(
                "app.services.decision_postgres.decide.get_query_analyzer"
            ) as getter,
        ):
            getter.return_value.analyze = analyze
            await decide(
                repo,
                query="QQ 항목",
                include_matched_columns=False,
                column_top_m=None,
                auto_resolve_entities=True,
            )
        self.assertLess(order.index("analyze"), order.index("glossary"))
        self.assertLess(order.index("analyze"), order.index("mappings"))
        self.assertLess(order.index("analyze"), order.index("catalog"))

    async def test_decide_seed_excludes_unchosen_prefix_tables(self) -> None:
        rows = [
            {
                "id": 1,
                "table_id": 1,
                "original_name": "DIM_TB",
                "name": "DIM_TB",
                "logical_name": "AA장",
                "subject_area": "master",
                "schema_name": "S",
                "natural_value": "AA장",
                "code_value": "1",
                "column_fqn": "S.DIM_TB.CODE",
                "matched_mention": "aa장",
            },
            {
                "id": 99,
                "table_id": 99,
                "original_name": "TMP_TB",
                "name": "TMP_TB",
                "logical_name": "AA 임시",
                "subject_area": "code",
                "schema_name": "S",
                "natural_value": "AA 임시1",
                "code_value": "981",
                "column_fqn": "S.TMP_TB.CODE",
                "matched_mention": "aa",
            },
            {
                "id": 99,
                "table_id": 99,
                "original_name": "TMP_TB",
                "name": "TMP_TB",
                "logical_name": "AA 임시",
                "subject_area": "code",
                "schema_name": "S",
                "natural_value": "AA 임시2",
                "code_value": "982",
                "column_fqn": "S.TMP_TB.CODE",
                "matched_mention": "aa",
            },
        ]
        repo = AsyncMock()
        repo.find_glossary_routes = AsyncMock(return_value=[])
        repo.find_value_mappings = AsyncMock(return_value=rows)
        repo.find_catalog_by_mentions = AsyncMock(return_value=[])
        repo.fk_neighbor_table_ids = AsyncMock(return_value=set())
        repo.fetch_tables_by_ids = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "original_name": "DIM_TB",
                    "name": "DIM_TB",
                    "logical_name": "AA장",
                    "subject_area": "master",
                    "schema_name": "S",
                },
                {
                    "id": 99,
                    "original_name": "TMP_TB",
                    "name": "TMP_TB",
                    "logical_name": "AA 임시",
                    "subject_area": "code",
                    "schema_name": "S",
                },
            ]
        )
        repo.fetch_join_edges = AsyncMock(return_value=[])
        repo.fetch_approved_columns = AsyncMock(return_value={})
        repo.execution_source_scope = AsyncMock(return_value=None)

        async def analyze(question: str, timeout_s=None, store_hits=None):
            return QueryAnalysis(
                status="complete",
                goal="x",
                measurement=MeasurementRequirement(),
                schema_roles=[],
            )

        with (
            patch(
                "app.services.decision_postgres.decide.get_runtime",
                return_value=_runtime(),
            ),
            patch(
                "app.services.decision_postgres.decide.get_query_analyzer"
            ) as getter,
        ):
            getter.return_value.analyze = analyze
            response = await decide(
                repo,
                query="AA장 항목",
                include_matched_columns=False,
                column_top_m=None,
                auto_resolve_entities=True,
            )
        names = {table.table_name for table in response.query_plan.required_tables}
        self.assertEqual(names, {"DIM_TB"})
        self.assertNotIn("TMP_TB", names)

    async def test_decide_keeps_seeds_when_fact_not_chosen(self) -> None:
        dim = {
            "id": 1,
            "original_name": "DIM_TB",
            "name": "DIM_TB",
            "logical_name": "항목 마스터",
            "subject_area": "master",
            "schema_name": "S",
            "table_id": 1,
            "natural_value": "QQ항목",
            "code_value": "Q1",
            "column_fqn": "S.DIM_TB.CODE",
            "matched_mention": "qq항목",
        }
        facts = [
            {
                "id": 2,
                "original_name": "FACT_A",
                "name": "FACT_A",
                "logical_name": "일 DATA A",
                "description": "일별 01dd",
                "subject_area": "agg",
                "schema_name": "S",
            },
            {
                "id": 3,
                "original_name": "FACT_B",
                "name": "FACT_B",
                "logical_name": "일 DATA B",
                "description": "일별 01dd",
                "subject_area": "agg",
                "schema_name": "S",
            },
        ]
        repo = AsyncMock()
        repo.find_glossary_routes = AsyncMock(return_value=[])
        repo.find_value_mappings = AsyncMock(return_value=[dim])
        repo.find_catalog_by_mentions = AsyncMock(return_value=[])
        repo.fk_neighbor_table_ids = AsyncMock(return_value={2, 3})
        repo.fetch_tables_by_ids = AsyncMock(return_value=[dim, *facts])
        repo.fetch_join_edges = AsyncMock(return_value=[])
        repo.fetch_approved_columns = AsyncMock(return_value={})
        repo.execution_source_scope = AsyncMock(return_value=None)

        async def analyze(question: str, timeout_s=None, store_hits=None):
            return QueryAnalysis(
                status="complete",
                goal="x",
                procedure="aggregate",
                period="2024년",
                metric="항목",
                meaning_status="complete",
                measurement=MeasurementRequirement(metric="항목"),
                schema_roles=[],
            )

        async def no_fact(query, analysis, candidates):
            return None

        set_fact_chooser(no_fact)
        try:
            with (
                patch(
                    "app.services.decision_postgres.decide.get_runtime",
                    return_value=_runtime(),
                ),
                patch(
                    "app.services.decision_postgres.decide.get_query_analyzer"
                ) as getter,
            ):
                getter.return_value.analyze = analyze
                response = await decide(
                    repo,
                    query="2024년 QQ항목 조회",
                    include_matched_columns=False,
                    column_top_m=None,
                    auto_resolve_entities=True,
                )
        finally:
            reset_fact_chooser()
        names = {table.table_name for table in response.query_plan.required_tables}
        self.assertEqual(names, {"DIM_TB"})
        self.assertTrue(
            any(
                "팩트" in item
                for item in (response.query_plan.unresolved_requirements or [])
            )
        )
        facts = [
            item
            for item in response.query_plan.candidate_evidence
            if item.kind == "fact"
        ]
        self.assertGreaterEqual(len(facts), 2)
        self.assertTrue(all(item.reason.strip() for item in facts))
        self.assertTrue(all(not item.selected for item in facts))
        self.assertTrue(all(item.match_type for item in facts))

    async def test_decide_uses_store_fact_choice(self) -> None:
        dim = {
            "id": 1,
            "original_name": "DIM_TB",
            "name": "DIM_TB",
            "logical_name": "항목 마스터",
            "subject_area": "master",
            "schema_name": "S",
            "table_id": 1,
            "natural_value": "QQ항목",
            "code_value": "Q1",
            "column_fqn": "S.DIM_TB.CODE",
            "matched_mention": "qq항목",
        }
        month = {
            "id": 2,
            "original_name": "FACT_A",
            "name": "FACT_A",
            "logical_name": "월 DATA",
            "description": "월 집계",
            "subject_area": "agg",
            "schema_name": "S",
        }
        day = {
            "id": 3,
            "original_name": "FACT_B",
            "name": "FACT_B",
            "logical_name": "일 DATA",
            "description": "일 집계",
            "subject_area": "agg",
            "schema_name": "S",
        }
        repo = AsyncMock()
        repo.find_glossary_routes = AsyncMock(return_value=[])
        repo.find_value_mappings = AsyncMock(return_value=[dim])
        repo.find_catalog_by_mentions = AsyncMock(return_value=[])
        repo.fk_neighbor_table_ids = AsyncMock(return_value={2, 3})
        repo.fetch_tables_by_ids = AsyncMock(return_value=[dim, month, day])
        repo.fetch_join_edges = AsyncMock(return_value=[])
        repo.fetch_approved_columns = AsyncMock(return_value={})
        repo.execution_source_scope = AsyncMock(return_value=None)

        async def analyze(question: str, timeout_s=None, store_hits=None):
            return QueryAnalysis(
                status="complete",
                goal="x",
                procedure="aggregate",
                period="2024년 5월 10일",
                metric="항목",
                meaning_status="complete",
                measurement=MeasurementRequirement(metric="항목"),
                schema_roles=[],
            )

        async def pick_month(query, analysis, candidates):
            return next(item for item in candidates if int(item["id"]) == 2)

        set_fact_chooser(pick_month)
        try:
            with (
                patch(
                    "app.services.decision_postgres.decide.get_runtime",
                    return_value=_runtime(),
                ),
                patch(
                    "app.services.decision_postgres.decide.get_query_analyzer"
                ) as getter,
            ):
                getter.return_value.analyze = analyze
                response = await decide(
                    repo,
                    query="2024년 QQ항목 조회",
                    include_matched_columns=False,
                    column_top_m=None,
                    auto_resolve_entities=True,
                )
        finally:
            reset_fact_chooser()
        names = {table.table_name for table in response.query_plan.required_tables}
        self.assertEqual(names, {"DIM_TB", "FACT_B"})
        self.assertNotIn("FACT_A", names)
        self.assertFalse(
            any(
                "팩트" in item
                for item in (response.query_plan.unresolved_requirements or [])
            )
        )
        by_name = {
            item.table_name: item
            for item in response.query_plan.candidate_evidence
            if item.kind == "fact"
        }
        self.assertTrue(by_name["FACT_B"].selected)
        self.assertFalse(by_name["FACT_A"].selected)
        self.assertTrue(by_name["FACT_A"].reason.strip())
        self.assertTrue(by_name["FACT_B"].reason.strip())

    async def test_decide_adds_fact_time_column_with_period_filter(self) -> None:
        dim = {
            "id": 1,
            "original_name": "DIM_TB",
            "name": "DIM_TB",
            "logical_name": "항목 마스터",
            "subject_area": "master",
            "schema_name": "S",
            "table_id": 1,
            "natural_value": "QQ항목",
            "code_value": "Q1",
            "column_fqn": "S.DIM_TB.CODE",
            "matched_mention": "qq항목",
        }
        fact = {
            "id": 2,
            "original_name": "FACT_A",
            "name": "FACT_A",
            "logical_name": "집계 DATA",
            "description": "집계",
            "subject_area": "agg",
            "schema_name": "S",
        }
        repo = AsyncMock()
        repo.find_glossary_routes = AsyncMock(return_value=[])
        repo.find_value_mappings = AsyncMock(return_value=[dim])
        repo.find_catalog_by_mentions = AsyncMock(return_value=[])
        repo.fk_neighbor_table_ids = AsyncMock(return_value={2})
        repo.fetch_tables_by_ids = AsyncMock(return_value=[dim, fact])
        repo.fetch_join_edges = AsyncMock(return_value=[])
        repo.fetch_approved_columns = AsyncMock(
            return_value={
                2: [
                    {
                        "name": "MEAS_TM",
                        "dtype": "varchar",
                        "metadata": {"format_pattern": "YYYYMM"},
                    }
                ]
            }
        )
        repo.execution_source_scope = AsyncMock(return_value=None)

        async def analyze(question: str, timeout_s=None, store_hits=None):
            return QueryAnalysis(
                status="complete",
                goal="x",
                procedure="aggregate",
                period="2024년",
                metric="항목",
                meaning_status="complete",
                measurement=MeasurementRequirement(metric="항목"),
                schema_roles=[],
            )

        with (
            patch(
                "app.services.decision_postgres.decide.get_runtime",
                return_value=_runtime(),
            ),
            patch(
                "app.services.decision_postgres.decide.get_query_analyzer"
            ) as getter,
        ):
            getter.return_value.analyze = analyze
            response = await decide(
                repo,
                query="QQ항목 조회",
                include_matched_columns=False,
                column_top_m=None,
                auto_resolve_entities=True,
            )
        fact_tables = [
            table
            for table in response.query_plan.required_tables
            if table.table_name == "FACT_A"
        ]
        self.assertEqual(len(fact_tables), 1)
        self.assertIn("MEAS_TM", fact_tables[0].required_columns)
        period_filters = [
            item
            for item in response.query_plan.filters
            if item.meaning == "측정 기간"
        ]
        self.assertEqual(len(period_filters), 1)
        self.assertEqual(period_filters[0].resolution_status, "resolved")
        self.assertIsNotNone(response.query_plan.aggregation)
        self.assertIn("2024", response.query_plan.aggregation.time_scope or "")

    async def test_decide_keeps_group_dimension_without_value_mapping(self) -> None:
        dim = {
            "id": 9,
            "original_name": "HQ_TB",
            "name": "HQ_TB",
            "logical_name": "지역본부",
            "description": "본부 마스터",
            "subject_area": "master",
            "schema_name": "S",
        }
        repo = AsyncMock()
        repo.find_glossary_routes = AsyncMock(return_value=[])
        repo.find_value_mappings = AsyncMock(return_value=[])
        repo.find_catalog_by_mentions = AsyncMock(return_value=[dim])
        repo.fk_neighbor_table_ids = AsyncMock(return_value=set())
        repo.fetch_tables_by_ids = AsyncMock(return_value=[dim])
        repo.fetch_join_edges = AsyncMock(return_value=[])
        repo.fetch_approved_columns = AsyncMock(return_value={})
        repo.execution_source_scope = AsyncMock(return_value=None)

        async def analyze(question: str, timeout_s=None, store_hits=None):
            return QueryAnalysis(
                status="complete",
                goal="x",
                measurement=MeasurementRequirement(),
                schema_roles=[],
            )

        with (
            patch(
                "app.services.decision_postgres.decide.get_runtime",
                return_value=_runtime(),
            ),
            patch(
                "app.services.decision_postgres.decide.get_query_analyzer"
            ) as getter,
        ):
            getter.return_value.analyze = analyze
            response = await decide(
                repo,
                query="본부별로 집계해줘",
                include_matched_columns=False,
                column_top_m=None,
                auto_resolve_entities=True,
            )
        names = {table.table_name for table in response.query_plan.required_tables}
        self.assertIn("HQ_TB", names)

    async def test_decide_list_does_not_select_unselected_join_hops(self) -> None:
        hq = {
            "id": 20,
            "original_name": "HQ_TB",
            "name": "HQ_TB",
            "logical_name": "지역본부",
            "subject_area": "master",
            "schema_name": "S",
            "table_id": 20,
            "natural_value": "금강유역본부",
            "code_value": "902",
            "column_fqn": "S.HQ_TB.BNB_CODE",
            "column_name": "BNB_CODE",
            "matched_mention": "금강권역",
        }
        plant = {
            "id": 30,
            "original_name": "PLANT_TB",
            "name": "PLANT_TB",
            "logical_name": "사업장",
            "description": "정수장 마스터",
            "subject_area": "master",
            "schema_name": "S",
            "table_id": 30,
            "natural_value": "금강유역본부",
            "code_value": "902",
            "column_fqn": "S.PLANT_TB.BNB_CODE",
            "column_name": "BNB_CODE",
            "matched_mention": "금강권역",
        }
        extra = {
            "id": 40,
            "original_name": "EXTRA_TB",
            "name": "EXTRA_TB",
            "logical_name": "코드사전",
            "subject_area": "code",
            "schema_name": "S",
        }
        repo = AsyncMock()
        repo.find_glossary_routes = AsyncMock(return_value=[])
        repo.find_value_mappings = AsyncMock(return_value=[hq, plant])
        repo.find_catalog_by_mentions = AsyncMock(return_value=[plant])
        repo.fk_neighbor_table_ids = AsyncMock(return_value={40})
        repo.fetch_tables_by_ids = AsyncMock(return_value=[hq, plant, extra])
        repo.fetch_join_edges = AsyncMock(
            return_value=[
                _fk_edge(20, 40, "HQ_TB", "EXTRA_TB", "BNB_CODE", "BNB_CODE"),
                _fk_edge(40, 30, "EXTRA_TB", "PLANT_TB", "BNB_CODE", "BNB_CODE"),
            ]
        )
        repo.fetch_approved_columns = AsyncMock(
            return_value={
                20: [{"name": "BNB_CODE", "metadata": {"column_name_kr": "본부코드"}}],
                30: [
                    {"name": "BNB_CODE", "metadata": {"column_name_kr": "본부코드"}},
                    {"name": "SUJ_NAME", "metadata": {"column_name_kr": "사업장명"}},
                ],
                40: [{"name": "BNB_CODE"}],
            }
        )
        repo.execution_source_scope = AsyncMock(return_value=None)

        async def analyze(question: str, timeout_s=None, store_hits=None):
            return QueryAnalysis(
                status="complete",
                goal="x",
                procedure="list",
                target="금강권역",
                primary_outputs=["정수장"],
                meaning_status="complete",
                measurement=MeasurementRequirement(),
                schema_roles=[],
            )

        with (
            patch(
                "app.services.decision_postgres.decide.get_runtime",
                return_value=_runtime(),
            ),
            patch(
                "app.services.decision_postgres.decide.get_query_analyzer"
            ) as getter,
        ):
            getter.return_value.analyze = analyze
            response = await decide(
                repo,
                query="금강권역 정수장 목록",
                include_matched_columns=False,
                column_top_m=None,
                auto_resolve_entities=True,
            )
        names = {table.table_name for table in response.query_plan.required_tables}
        self.assertIn("HQ_TB", names)
        self.assertIn("PLANT_TB", names)
        self.assertNotIn("EXTRA_TB", names)
        self.assertEqual(response.query_plan.completeness, "complete")
        self.assertFalse(
            any(
                "승인 JOIN 경로 없음" in str(item)
                for item in (response.query_plan.unresolved_requirements or [])
            )
        )
        evidence = response.query_plan.candidate_evidence
        self.assertTrue(evidence)
        self.assertTrue(all(item.reason.strip() for item in evidence))
        plant = next(
            item
            for item in evidence
            if item.kind == "catalog" and item.table_name == "PLANT_TB"
        )
        self.assertTrue(plant.selected)
        mappings = [item for item in evidence if item.kind == "value_mapping"]
        self.assertTrue(mappings)
        self.assertTrue(any(item.selected for item in mappings))

    async def test_decide_aggregate_without_period_asks_for_period(self) -> None:
        repo = AsyncMock()

        async def analyze(question: str, timeout_s=None, store_hits=None):
            return QueryAnalysis(
                status="complete",
                goal="x",
                procedure="aggregate",
                metric="탁도",
                meaning_status="complete",
                measurement=MeasurementRequirement(metric="탁도", aggregation="AVG"),
                schema_roles=[],
            )

        with (
            patch(
                "app.services.decision_postgres.decide.get_runtime",
                return_value=_runtime(),
            ),
            patch(
                "app.services.decision_postgres.decide.get_query_analyzer"
            ) as getter,
        ):
            getter.return_value.analyze = analyze
            response = await decide(
                repo,
                query="화성정수장 평균 탁도",
                include_matched_columns=False,
                column_top_m=None,
                auto_resolve_entities=True,
            )
        self.assertEqual(response.query_plan.completeness, "failed")
        self.assertEqual(
            response.query_plan.unresolved_requirements, [PERIOD_REQUIRED]
        )
        repo.find_value_mappings.assert_not_called()

    async def test_decide_lookup_average_without_period_asks_for_period(self) -> None:
        repo = AsyncMock()

        async def analyze(question: str, timeout_s=None, store_hits=None):
            return QueryAnalysis(
                status="complete",
                goal="x",
                procedure="lookup",
                metric="평균 탁도",
                meaning_status="complete",
                measurement=MeasurementRequirement(metric="평균 탁도"),
                schema_roles=[],
            )

        with (
            patch(
                "app.services.decision_postgres.decide.get_runtime",
                return_value=_runtime(),
            ),
            patch(
                "app.services.decision_postgres.decide.get_query_analyzer"
            ) as getter,
        ):
            getter.return_value.analyze = analyze
            response = await decide(
                repo,
                query="화성정수장 평균 탁도",
                include_matched_columns=False,
                column_top_m=None,
                auto_resolve_entities=True,
            )
        self.assertEqual(response.query_plan.completeness, "failed")
        self.assertEqual(
            response.query_plan.unresolved_requirements, [PERIOD_REQUIRED]
        )
        repo.find_value_mappings.assert_not_called()

    async def test_decide_lookup_metric_without_period_asks_for_period(self) -> None:
        repo = AsyncMock()

        async def analyze(question: str, timeout_s=None, store_hits=None):
            return QueryAnalysis(
                status="complete",
                goal="x",
                procedure="lookup",
                target="충주",
                metric="공급량",
                meaning_status="complete",
                measurement=MeasurementRequirement(metric="공급량"),
                schema_roles=[],
            )

        with (
            patch(
                "app.services.decision_postgres.decide.get_runtime",
                return_value=_runtime(),
            ),
            patch(
                "app.services.decision_postgres.decide.get_query_analyzer"
            ) as getter,
        ):
            getter.return_value.analyze = analyze
            response = await decide(
                repo,
                query="충주 취수 공급량은?",
                include_matched_columns=False,
                column_top_m=None,
                auto_resolve_entities=True,
            )
        self.assertEqual(response.query_plan.completeness, "failed")
        self.assertEqual(
            response.query_plan.unresolved_requirements, [PERIOD_REQUIRED]
        )
        repo.find_value_mappings.assert_not_called()

    async def test_decide_rewrites_mapping_filters_onto_wide_fact(self) -> None:
        dim = {
            "id": 1,
            "table_id": 1,
            "original_name": "vw_tag_dim",
            "name": "vw_tag_dim",
            "logical_name": "태그 마스터",
            "subject_area": "master",
            "schema_name": "rwis_mart",
            "natural_value": "충주정수장",
            "code_value": "380",
            "column_fqn": "rwis_mart.vw_tag_dim.suj_code",
            "column_name": "suj_code",
            "matched_mention": "충주정수장",
        }
        metric = {
            "id": 1,
            "table_id": 1,
            "original_name": "vw_tag_dim",
            "name": "vw_tag_dim",
            "logical_name": "태그 마스터",
            "subject_area": "master",
            "schema_name": "rwis_mart",
            "natural_value": "탁도",
            "code_value": "TB",
            "column_fqn": "rwis_mart.vw_tag_dim.br_code",
            "column_name": "br_code",
            "matched_mention": "탁도",
        }
        day = {
            "id": 2,
            "original_name": "vw_measure_day",
            "name": "vw_measure_day",
            "logical_name": "일 DATA",
            "description": "일별 01dd",
            "subject_area": "agg",
            "schema_name": "rwis_mart",
        }
        repo = AsyncMock()
        repo.find_glossary_routes = AsyncMock(return_value=[])
        repo.find_value_mappings = AsyncMock(return_value=[dim, metric])
        repo.find_catalog_by_mentions = AsyncMock(return_value=[])
        repo.fk_neighbor_table_ids = AsyncMock(return_value={2})
        repo.fetch_tables_by_ids = AsyncMock(return_value=[dim, day])
        repo.list_serving_tables = AsyncMock(return_value=[day])
        repo.fetch_join_edges = AsyncMock(return_value=[])
        repo.fetch_approved_columns = AsyncMock(
            return_value={
                1: [
                    {"name": "suj_code"},
                    {"name": "br_code"},
                    {"name": "tagsn", "is_primary_key": True},
                ],
                2: [
                    {"name": "suj_code"},
                    {"name": "br_code"},
                    {
                        "name": "meas_tm",
                        "dtype": "varchar",
                        "metadata": {"format_pattern": "YYYYMMDD"},
                    },
                    {"name": "val"},
                ],
            }
        )
        repo.execution_source_scope = AsyncMock(return_value=None)

        async def analyze(question: str, timeout_s=None, store_hits=None):
            return QueryAnalysis(
                status="complete",
                goal="x",
                procedure="lookup",
                period="2025년 8월",
                metric="탁도",
                target="충주정수장",
                primary_outputs=["탁도"],
                meaning_status="complete",
                measurement=MeasurementRequirement(metric="탁도"),
                schema_roles=[],
            )

        with (
            patch(
                "app.services.decision_postgres.decide.get_runtime",
                return_value=_runtime(),
            ),
            patch(
                "app.services.decision_postgres.decide.get_query_analyzer"
            ) as getter,
        ):
            getter.return_value.analyze = analyze
            response = await decide(
                repo,
                query="충주정수장 2025년 8월 탁도변화",
                include_matched_columns=False,
                column_top_m=None,
                auto_resolve_entities=True,
            )
        names = {table.table_name for table in response.query_plan.required_tables}
        self.assertEqual(names, {"vw_measure_day"})
        self.assertFalse(response.query_plan.join_paths)
        self.assertFalse(
            any(
                "승인 JOIN 경로 없음" in str(item)
                for item in (response.query_plan.unresolved_requirements or [])
            )
        )
        columns = {
            item.column
            for item in response.query_plan.filters
            if item.resolution_status == "resolved" and item.column
        }
        self.assertIn("rwis_mart.vw_measure_day.suj_code", columns)
        self.assertIn("rwis_mart.vw_measure_day.br_code", columns)

    async def test_decide_keeps_mapping_table_when_fact_lacks_code_column(self) -> None:
        dim = {
            "id": 1,
            "table_id": 1,
            "original_name": "RDISAUP_TB",
            "name": "RDISAUP_TB",
            "logical_name": "사업장",
            "subject_area": "master",
            "schema_name": "S",
            "source_instance_id": "rwis-pg",
            "natural_value": "충주정수장",
            "code_value": "380",
            "column_fqn": "S.RDISAUP_TB.SUJ_CODE",
            "column_name": "SUJ_CODE",
            "matched_mention": "충주정수장",
        }
        fact = {
            "id": 2,
            "original_name": "RDD01DD_TB",
            "name": "RDD01DD_TB",
            "logical_name": "일 DATA",
            "description": "일별 01dd",
            "subject_area": "agg",
            "schema_name": "S",
            "source_instance_id": "rwis-pg",
        }
        repo = AsyncMock()
        repo.find_glossary_routes = AsyncMock(return_value=[])
        repo.find_value_mappings = AsyncMock(return_value=[dim])
        repo.find_catalog_by_mentions = AsyncMock(return_value=[])
        repo.fk_neighbor_table_ids = AsyncMock(return_value={2})
        repo.fetch_tables_by_ids = AsyncMock(return_value=[dim, fact])
        repo.list_serving_tables = AsyncMock(return_value=[fact])
        repo.fetch_join_edges = AsyncMock(
            return_value=[
                _fk_edge(1, 2, "RDISAUP_TB", "RDD01DD_TB", "SUJ_CODE", "SUJ_CODE"),
            ]
        )
        repo.fetch_approved_columns = AsyncMock(
            return_value={
                1: [{"name": "SUJ_CODE", "metadata": {"column_name_kr": "사업장코드"}}],
                2: [
                    {"name": "VAL"},
                    {
                        "name": "LOG_TIME",
                        "dtype": "varchar",
                        "metadata": {"format_pattern": "YYYYMMDD"},
                    },
                ],
            }
        )
        repo.execution_source_scope = AsyncMock(return_value=None)

        async def analyze(question: str, timeout_s=None, store_hits=None):
            return QueryAnalysis(
                status="complete",
                goal="x",
                procedure="lookup",
                period="2025년 8월",
                metric="탁도",
                meaning_status="complete",
                measurement=MeasurementRequirement(metric="탁도"),
                schema_roles=[],
            )

        with (
            patch(
                "app.services.decision_postgres.decide.get_runtime",
                return_value=_runtime(),
            ),
            patch(
                "app.services.execution_context_resolver.get_runtime",
                return_value=_runtime(),
            ),
            patch(
                "app.services.decision_postgres.decide.get_query_analyzer"
            ) as getter,
        ):
            getter.return_value.analyze = analyze
            response = await decide(
                repo,
                query="충주정수장 2025년 8월 탁도",
                include_matched_columns=False,
                column_top_m=None,
                auto_resolve_entities=True,
            )
        names = {table.table_name for table in response.query_plan.required_tables}
        self.assertEqual(names, {"RDISAUP_TB", "RDD01DD_TB"})
        self.assertTrue(response.query_plan.join_paths)
        self.assertFalse(
            any(
                "승인 JOIN 경로 없음" in str(item)
                for item in (response.query_plan.unresolved_requirements or [])
            )
        )
        columns = {
            item.column
            for item in response.query_plan.filters
            if item.resolution_status == "resolved" and item.column
        }
        self.assertTrue(any(str(col).endswith("RDISAUP_TB.SUJ_CODE") for col in columns))

    async def test_decide_plant_list_omits_tagsn(self) -> None:
        dim = {
            "id": 1,
            "table_id": 1,
            "original_name": "vw_tag_dim",
            "name": "vw_tag_dim",
            "logical_name": "태그 마스터",
            "subject_area": "master",
            "schema_name": "rwis_mart",
            "natural_value": "금강유역본부",
            "code_value": "701",
            "column_fqn": "rwis_mart.vw_tag_dim.bnb_code",
            "column_name": "bnb_code",
            "matched_mention": "금강권역",
        }
        repo = AsyncMock()
        repo.find_glossary_routes = AsyncMock(return_value=[])
        repo.find_value_mappings = AsyncMock(return_value=[dim])
        repo.find_catalog_by_mentions = AsyncMock(return_value=[dim])
        repo.fk_neighbor_table_ids = AsyncMock(return_value=set())
        repo.fetch_tables_by_ids = AsyncMock(return_value=[dim])
        repo.list_serving_tables = AsyncMock(return_value=[])
        repo.fetch_join_edges = AsyncMock(return_value=[])
        repo.fetch_approved_columns = AsyncMock(
            return_value={
                1: [
                    {
                        "name": "tagsn",
                        "is_primary_key": True,
                        "metadata": {"column_name_kr": "태그일련번호"},
                    },
                    {
                        "name": "suj_code",
                        "metadata": {"column_name_kr": "사업장코드"},
                    },
                    {
                        "name": "suj_name",
                        "metadata": {"column_name_kr": "사업장이름"},
                    },
                    {
                        "name": "bnb_code",
                        "metadata": {"column_name_kr": "유역본부코드"},
                    },
                ]
            }
        )
        repo.execution_source_scope = AsyncMock(return_value=None)

        async def analyze(question: str, timeout_s=None, store_hits=None):
            return QueryAnalysis(
                status="complete",
                goal="x",
                procedure="list",
                target="금강권역",
                primary_outputs=["정수장"],
                meaning_status="complete",
                measurement=MeasurementRequirement(),
                schema_roles=[],
            )

        with (
            patch(
                "app.services.decision_postgres.decide.get_runtime",
                return_value=_runtime(),
            ),
            patch(
                "app.services.decision_postgres.decide.get_query_analyzer"
            ) as getter,
        ):
            getter.return_value.analyze = analyze
            response = await decide(
                repo,
                query="금강권역 정수장 목록",
                include_matched_columns=False,
                column_top_m=None,
                auto_resolve_entities=True,
            )
        tables = response.query_plan.required_tables
        self.assertEqual({table.table_name for table in tables}, {"vw_tag_dim"})
        columns = set(tables[0].required_columns)
        self.assertIn("suj_code", columns)
        self.assertIn("suj_name", columns)
        self.assertNotIn("tagsn", columns)
        self.assertFalse(response.query_plan.join_paths)


if __name__ == "__main__":
    unittest.main()
