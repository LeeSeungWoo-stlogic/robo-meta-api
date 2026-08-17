from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.schemas import MeasurementRequirement, QueryAnalysis
from app.services.decision_postgres.decide import decide
from app.services.decision_postgres.fact_choose import (
    reset_fact_chooser,
    set_fact_chooser,
)
from app.services.decision_postgres.period import parse_korean_period
from app.services.decision_postgres.store_first import (
    dimension_identity_column_names,
    fact_time_column_names,
    filter_mappings_to_labels,
    is_category_mention,
    is_groupby_mention,
    is_tag_master_table,
    mapping_filters,
    measure_point_label_filters,
    partition_mention_mappings,
    period_filter_for_fact,
    pick_fact_tables,
    promote_series_identity_tables,
    query_requests_fact,
)
from app.services.metadata_repository._search import SearchMixin


def _runtime() -> MagicMock:
    runtime = MagicMock()
    runtime.decision.table_top_k = 10
    runtime.decision.fk_max_hops = 3
    return runtime


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

    def test_list_query_does_not_request_fact(self) -> None:
        self.assertFalse(query_requests_fact("금강권역 정수장 목록", None))
        self.assertFalse(
            query_requests_fact("아산정수장에서 측정되고 있는 데이터들은 어떤 게 있어?", None)
        )
        period = parse_korean_period("25년 10월 잔류염소")
        self.assertTrue(query_requests_fact("25년 10월 잔류염소", period))
        listed = parse_korean_period("2025년 10월 사업장 목록")
        self.assertFalse(query_requests_fact("2025년 10월 사업장 목록", listed))

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
        self.assertEqual({int(item["id"]) for item in kept}, {1, 2})
        self.assertIsNotNone(kept_err)
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
        self.assertEqual({int(item["id"]) for item in facts_only}, {1, 2})
        self.assertIsNotNone(raw_err)
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
        self.assertEqual(set(names), {"TAGSN", "TAG_DESC", "TAG_NAME"})

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
        )
        self.assertEqual(still_bridge, {3})

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
        self.assertEqual(len(likes), 1)
        self.assertEqual(likes[0].value, "%탁도%")
        self.assertEqual(likes[0].column, "RWIS.POINT_MASTER.TAG_DESC")
        self.assertEqual(len(unused), 1)
        self.assertEqual(unused[0].value, "%사용안함%")
        self.assertNotIn("충주", likes[0].value)

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


class StoreFirstDecideTests(unittest.IsolatedAsyncioTestCase):
    async def test_decide_empty_seed_returns_no_meta(self) -> None:
        repo = AsyncMock()
        repo.find_glossary_routes = AsyncMock(return_value=[])
        repo.find_value_mappings = AsyncMock(return_value=[])
        repo.find_catalog_by_mentions = AsyncMock(return_value=[])
        analyze = AsyncMock()
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
        analyze.assert_not_called()

    async def test_decide_calls_store_before_analyze(self) -> None:
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

        async def analyze(question: str, store_hits=None):
            order.append("analyze")
            self.assertIsNotNone(store_hits)
            assert store_hits is not None
            self.assertIn("catalog", store_hits)
            return QueryAnalysis(
                status="complete",
                intent="x",
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
        self.assertLess(order.index("glossary"), order.index("analyze"))
        self.assertLess(order.index("mappings"), order.index("analyze"))
        self.assertLess(order.index("catalog"), order.index("analyze"))

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

        async def analyze(question: str, store_hits=None):
            return QueryAnalysis(
                status="complete",
                intent="x",
                entities_include=["AA장"],
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
                "logical_name": "월 DATA",
                "description": "월 집계",
                "subject_area": "agg",
                "schema_name": "S",
            },
            {
                "id": 3,
                "original_name": "FACT_B",
                "name": "FACT_B",
                "logical_name": "일 DATA",
                "description": "일 집계",
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

        async def analyze(question: str, store_hits=None):
            return QueryAnalysis(
                status="complete",
                intent="x",
                entities_include=["QQ항목"],
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
                    query="QQ항목 조회",
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

        async def analyze(question: str, store_hits=None):
            return QueryAnalysis(
                status="complete",
                intent="x",
                entities_include=["QQ항목"],
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
                    query="QQ항목 조회",
                    include_matched_columns=False,
                    column_top_m=None,
                    auto_resolve_entities=True,
                )
        finally:
            reset_fact_chooser()
        names = {table.table_name for table in response.query_plan.required_tables}
        self.assertEqual(names, {"DIM_TB"})
        self.assertNotIn("FACT_A", names)
        self.assertNotIn("FACT_B", names)
        self.assertTrue(
            any(
                "팩트" in item
                for item in (response.query_plan.unresolved_requirements or [])
            )
        )

    async def test_decide_adds_fact_time_column_without_period_filter(self) -> None:
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

        async def analyze(question: str, store_hits=None):
            return QueryAnalysis(
                status="complete",
                intent="x",
                entities_include=["QQ항목"],
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
        self.assertEqual(period_filters, [])

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

        async def analyze(question: str, store_hits=None):
            return QueryAnalysis(
                status="complete",
                intent="x",
                entities_include=[],
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


if __name__ == "__main__":
    unittest.main()
