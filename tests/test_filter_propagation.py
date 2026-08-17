from __future__ import annotations

import unittest

from app.services.decision_postgres.filters import (
    _mapping_matches_value_text,
    _period_from_requirement,
    _propagate_filters_along_fk,
    _resolve_filters,
    _surface_value_may_resolve,
    _with_mapping_filter_requirements,
    _with_metric_filter_requirements,
)
from app.services.decision_postgres.period import parse_korean_period
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

    def test_plant_label_with_particle_is_standalone(self) -> None:
        self.assertTrue(
            SearchMixin._natural_value_is_standalone_mention(
                "2025년 공주정수장의 월별 TOC 농도 평균 알려줘",
                "공주정수장",
            )
        )
        self.assertTrue(
            SearchMixin._natural_value_is_standalone_mention(
                "2025년 9월 낙동강에서 강우량이 가장 많은 곳이 어디야?",
                "낙동강",
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


class MappingFilterRequirementTests(unittest.TestCase):
    def test_promotes_verified_mapping_without_analyzer_slot(self) -> None:
        analysis = QueryAnalysis(status="complete")
        mappings = [
            {
                "natural_value": "충청지역",
                "code_value": "902",
                "column_fqn": "rwis.RWIS.RDISAUP_TB.SUJ_CODE",
            }
        ]
        requirements = _with_mapping_filter_requirements(analysis, mappings)
        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0].value_text, "충청지역")
        self.assertTrue(requirements[0].meaning.startswith("코드매핑:"))

    def test_skips_incomplete_or_duplicate_mapping(self) -> None:
        analysis = QueryAnalysis(
            status="complete",
            filter_requirements=[
                FilterRequirement(meaning="지역", value_text="충청지역"),
            ],
        )
        mappings = [
            {"natural_value": "충청지역", "code_value": "902"},
            {
                "natural_value": "충청지역",
                "code_value": "902",
                "column_fqn": "rwis.RWIS.RDISAUP_TB.SUJ_CODE",
            },
        ]
        self.assertEqual(len(_with_mapping_filter_requirements(analysis, mappings)), 1)


class ResolveMappedFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolved_filter_uses_store_code_value(self) -> None:
        planned, unresolved = await _resolve_filters(
            repository=None,
            requirements=[
                FilterRequirement(
                    meaning="코드매핑:충청지역",
                    value_text="충청지역",
                )
            ],
            embeddings={},
            table_ids=[1],
            tables_by_id={
                1: {
                    "schema_name": "RWIS",
                    "original_name": "RDISAUP_TB",
                }
            },
            mappings=[
                {
                    "natural_value": "충청지역",
                    "code_value": "902",
                    "column_fqn": "rwis.RWIS.RDISAUP_TB.SUJ_CODE",
                }
            ],
            minimum_similarity=0.2,
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].value, "902")
        self.assertEqual(planned[0].column, "RWIS.RDISAUP_TB.SUJ_CODE")
        self.assertEqual(planned[0].resolution_status, "resolved")

    async def test_mapping_fqn_outside_plan_stays_unresolved(self) -> None:
        planned, unresolved = await _resolve_filters(
            repository=None,
            requirements=[
                FilterRequirement(
                    meaning="코드매핑:충청지역",
                    value_text="충청지역",
                )
            ],
            embeddings={},
            table_ids=[2],
            tables_by_id={
                2: {
                    "schema_name": "RWIS",
                    "original_name": "RDITAG_TB",
                }
            },
            mappings=[
                {
                    "natural_value": "충청지역",
                    "code_value": "902",
                    "column_fqn": "rwis.RWIS.RDISAUP_TB.SUJ_CODE",
                }
            ],
            minimum_similarity=0.2,
        )
        self.assertEqual(planned[0].resolution_status, "unresolved")
        self.assertTrue(unresolved)


class _ColumnSearchRepo:
    def __init__(self, column: dict) -> None:
        self.column = column

    async def search_columns(self, embedding, *, table_ids, per_table_limit):
        return {int(self.column["table_id"]): [self.column]}


class ResolveSurfaceFilterTests(unittest.IsolatedAsyncioTestCase):
    async def _resolve(self, *, column: dict, value_text: str, meaning: str):
        table_id = int(column["table_id"])
        return await _resolve_filters(
            repository=_ColumnSearchRepo(column),
            requirements=[
                FilterRequirement(meaning=meaning, value_text=value_text)
            ],
            embeddings={"filter:0": [0.1, 0.2]},
            table_ids=[table_id],
            tables_by_id={
                table_id: {
                    "schema_name": "SCHEMA_A",
                    "original_name": "TABLE_A",
                }
            },
            mappings=[],
            minimum_similarity=0.2,
        )

    async def test_hangul_on_numeric_column_stays_unresolved(self) -> None:
        planned, unresolved = await self._resolve(
            column={
                "table_id": 1,
                "name": "TAG_FULL",
                "dtype": "numeric",
                "score": 0.8,
                "description": "",
            },
            value_text="금강권역",
            meaning="권역",
        )
        self.assertEqual(planned[0].resolution_status, "unresolved")
        self.assertIsNone(planned[0].column)
        self.assertTrue(unresolved)
        self.assertIn("권역", unresolved[0])

    async def test_hangul_on_code_column_without_mapping_stays_unresolved(self) -> None:
        planned, unresolved = await self._resolve(
            column={
                "table_id": 1,
                "name": "SUJ_CODE",
                "dtype": "character",
                "score": 0.9,
                "description": "",
            },
            value_text="금강권역",
            meaning="권역",
        )
        self.assertEqual(planned[0].resolution_status, "unresolved")
        self.assertIsNone(planned[0].column)

    async def test_hangul_on_name_column_stays_unresolved_without_mapping(self) -> None:
        planned, unresolved = await self._resolve(
            column={
                "table_id": 1,
                "name": "SUJ_NAME",
                "dtype": "character varying",
                "score": 0.9,
                "description": "",
            },
            value_text="아산정수장",
            meaning="정수장 명칭",
        )
        self.assertEqual(planned[0].resolution_status, "unresolved")
        self.assertIsNone(planned[0].column)
        self.assertTrue(unresolved)

    async def test_alphabetic_metric_on_value_column_stays_unresolved(self) -> None:
        planned, unresolved = await self._resolve(
            column={
                "table_id": 1,
                "name": "VAL",
                "dtype": "numeric",
                "score": 0.9,
                "description": "측정값",
            },
            value_text="TOC",
            meaning="측정항목:TOC",
        )
        self.assertEqual(planned[0].resolution_status, "unresolved")
        self.assertIsNone(planned[0].column)
        self.assertTrue(unresolved)

    async def test_code_literal_on_code_column_stays_resolved(self) -> None:
        planned, unresolved = await self._resolve(
            column={
                "table_id": 1,
                "name": "SUJ_CODE",
                "dtype": "character",
                "score": 0.9,
                "description": "",
            },
            value_text="354",
            meaning="사업장 코드",
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(planned[0].resolution_status, "resolved")
        self.assertEqual(planned[0].value, "354")
        self.assertEqual(planned[0].column, "SCHEMA_A.TABLE_A.SUJ_CODE")

    async def test_sibling_year_does_not_bind_basin_to_time(self) -> None:
        table_id = 1
        column = {
            "table_id": table_id,
            "name": "LOG_TIME",
            "dtype": "character",
            "score": 0.9,
            "description": "로그 시각",
            "metadata": {"format_pattern": "YYYYMMDD"},
        }
        planned, _unresolved = await _resolve_filters(
            repository=_ColumnSearchRepo(column),
            requirements=[
                FilterRequirement(meaning="측정 기간", value_text="2025년 9월"),
                FilterRequirement(meaning="유역", value_text="낙동강"),
            ],
            embeddings={"filter:0": [0.1, 0.2], "filter:1": [0.1, 0.2]},
            table_ids=[table_id],
            tables_by_id={
                table_id: {
                    "schema_name": "SCHEMA_A",
                    "original_name": "TABLE_A",
                }
            },
            mappings=[],
            minimum_similarity=0.2,
        )
        likes = [item for item in planned if item.operator == "LIKE"]
        betweens = [item for item in planned if item.operator == "BETWEEN"]
        self.assertEqual(len(likes), 0)
        self.assertEqual(len(betweens), 1)
        self.assertEqual(betweens[0].value, "20250901,20250930")
        self.assertEqual(betweens[0].meaning, "측정 기간")
        self.assertEqual(planned[1].meaning, "유역")
        self.assertNotEqual(planned[1].operator, "LIKE")

    async def test_korean_period_binds_to_time_column(self) -> None:
        planned, unresolved = await self._resolve(
            column={
                "table_id": 1,
                "name": "OBS_TIME",
                "dtype": "character",
                "score": 0.4,
                "description": "관측 시각",
                "metadata": {"format_pattern": "YYYYMMDD"},
            },
            value_text="2025년 9월",
            meaning="측정 기간",
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(planned[0].resolution_status, "resolved")
        self.assertEqual(planned[0].operator, "BETWEEN")
        self.assertEqual(planned[0].value, "20250901,20250930")
        self.assertEqual(planned[0].column, "SCHEMA_A.TABLE_A.OBS_TIME")


class PeriodParseTests(unittest.TestCase):
    def test_fallback_year_needs_month_or_day_in_text(self) -> None:
        self.assertIsNone(parse_korean_period("낙동강", fallback_year=2025))
        self.assertIsNone(parse_korean_period("청주장", fallback_year=2025))
        parsed = parse_korean_period("10월", fallback_year=2025)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.like_prefix, "202510")

    def test_non_period_meaning_does_not_inherit_year(self) -> None:
        requirements = [
            FilterRequirement(meaning="측정 기간", value_text="2025년 9월"),
            FilterRequirement(meaning="유역", value_text="낙동강"),
            FilterRequirement(meaning="사업장 명칭", value_text="청주장"),
        ]
        self.assertIsNone(_period_from_requirement(requirements[1], requirements))
        self.assertIsNone(_period_from_requirement(requirements[2], requirements))
        period = _period_from_requirement(requirements[0], requirements)
        self.assertIsNotNone(period)
        self.assertEqual(period.like_prefix, "202509")

    def test_month_slot_inherits_sibling_year(self) -> None:
        requirements = [
            FilterRequirement(meaning="연도", value_text="2025년"),
            FilterRequirement(meaning="측정 월", value_text="10월"),
        ]
        period = _period_from_requirement(requirements[1], requirements)
        self.assertIsNotNone(period)
        self.assertEqual(period.like_prefix, "202510")


class MappingCodeMatchTests(unittest.TestCase):
    def test_metric_matches_mapping_code_exactly(self) -> None:
        self.assertTrue(
            _mapping_matches_value_text(
                {"natural_value": "수소이온농도", "code_value": "PH"},
                "ph",
            )
        )
        self.assertFalse(
            _mapping_matches_value_text(
                {"natural_value": "수소이온농도", "code_value": "PH"},
                "탁도",
            )
        )

    def test_question_token_matches_longer_store_label(self) -> None:
        self.assertTrue(
            _mapping_matches_value_text(
                {"natural_value": "팔당1취수장", "code_value": "211"},
                "팔당",
            )
        )
        self.assertTrue(
            _mapping_matches_value_text(
                {"natural_value": "강우량(Rainfall)", "code_value": "RA"},
                "강우량",
            )
        )
        self.assertTrue(
            SearchMixin._token_is_label_mention("낙동강", "낙동강유역본부")
        )
        self.assertFalse(
            SearchMixin._token_is_label_mention("정수", "청주정수장")
        )
        self.assertFalse(
            _mapping_matches_value_text(
                {"natural_value": "청주", "code_value": "880"},
                "청주정수장",
            )
        )

    def test_toc_does_not_match_organic_carbon_mapping(self) -> None:
        self.assertFalse(
            _mapping_matches_value_text(
                {"natural_value": "총 유기탄소", "code_value": "TC"},
                "TOC",
            )
        )

    def test_toc_matches_after_standard_word_surface(self) -> None:
        self.assertTrue(
            _mapping_matches_value_text(
                {
                    "natural_value": "총 유기탄소",
                    "code_value": "TC",
                    "matched_surfaces": ["TOC"],
                },
                "TOC",
            )
        )


class ReverseMentionSelectTests(unittest.TestCase):
    def test_keeps_reverse_hits_when_full_label_absent(self) -> None:
        rows = SearchMixin._select_mentioned_mappings(
            "팔당 취수량",
            [
                {"natural_value": "팔당1취수장", "code_value": "211", "column_fqn": "a.SUJ"},
                {"natural_value": "팔당권", "code_value": "700", "column_fqn": "a.SUJ"},
            ],
            ["팔당"],
        )
        labels = {row["natural_value"] for row in rows}
        self.assertEqual(labels, {"팔당1취수장", "팔당권"})

    def test_shorter_exact_label_drops_when_longer_exact_exists(self) -> None:
        rows = SearchMixin._select_mentioned_mappings(
            "청주정수장 월평균 PH",
            [
                {"natural_value": "청주", "code_value": "880", "column_fqn": "a.SUJ"},
                {
                    "natural_value": "청주정수장",
                    "code_value": "353",
                    "column_fqn": "a.SUJ",
                },
            ],
            ["청주", "청주정수장", "ph"],
        )
        labels = {row["natural_value"] for row in rows}
        self.assertEqual(labels, {"청주정수장"})

    def test_exact_plant_label_drops_shorter_prefix_alias(self) -> None:
        rows = SearchMixin._select_mentioned_mappings(
            "청주정수장 월평균 PH",
            [
                {
                    "natural_value": "청주정수장",
                    "code_value": "353",
                    "column_fqn": "a.SUJ",
                },
                {"natural_value": "청주권", "code_value": "880", "column_fqn": "a.SUJ"},
            ],
            ["청주", "청주정수장", "ph"],
        )
        labels = {row["natural_value"] for row in rows}
        self.assertEqual(labels, {"청주정수장"})

    def test_cheongju_nickname_does_not_invent_plant(self) -> None:
        rows = SearchMixin._select_mentioned_mappings(
            "청주장 월평균 ph",
            [
                {
                    "natural_value": "청주정수장",
                    "code_value": "353",
                    "column_fqn": "a.SUJ",
                }
            ],
            ["청주장"],
        )
        self.assertEqual(rows, [])

    def test_trusted_extra_expands_to_mapping_label(self) -> None:
        rows = SearchMixin._select_mentioned_mappings(
            "공주정수장 10월 TOC 농도",
            [
                {
                    "natural_value": "총 유기탄소",
                    "code_value": "TC",
                    "column_fqn": "a.BR",
                }
            ],
            ["toc", "총유기탄소"],
            trusted_extras={"총유기탄소"},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code_value"], "TC")

    def test_latin_abbreviation_is_word_bounded(self) -> None:
        self.assertTrue(
            SearchMixin._surface_is_standalone_mention(
                "공주정수장 10월 TOC 농도",
                "TOC",
            )
        )
        self.assertFalse(
            SearchMixin._surface_is_standalone_mention("PIPELINE 상태", "IP")
        )

    def test_stopwords_are_not_reverse_tokens(self) -> None:
        tokens = SearchMixin._question_mention_tokens(
            "강우량이 가장 많은 정수장",
        )
        self.assertIn("강우량", tokens)
        self.assertNotIn("가장", tokens)
        self.assertIn("정수장", tokens)

    def test_group_suffix_is_stripped(self) -> None:
        tokens = SearchMixin._question_mention_tokens("본부별 평균")
        self.assertIn("본부", tokens)
        self.assertNotIn("본부별", tokens)

    def test_aggregation_words_are_not_reverse_tokens(self) -> None:
        tokens = SearchMixin._question_mention_tokens(
            "유량 수위 강우량의 평균 최대값 최소값",
        )
        self.assertIn("유량", tokens)
        self.assertIn("수위", tokens)
        self.assertIn("강우량", tokens)
        self.assertNotIn("평균", tokens)
        self.assertNotIn("최대", tokens)
        self.assertNotIn("최대값", tokens)
        self.assertNotIn("최소값", tokens)


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


class MeasureBindGuardTests(unittest.TestCase):
    def test_numeric_threshold_does_not_bind_to_code_column(self) -> None:
        column = {"name": "SUJ_CODE", "dtype": "character"}
        self.assertFalse(
            _surface_value_may_resolve("0", column, "월 평균 ph")
        )
        self.assertTrue(
            _surface_value_may_resolve("354", column, "사업장 코드")
        )


class ResolvePeriodAndClusterTests(unittest.IsolatedAsyncioTestCase):
    async def test_period_prefers_measure_time_over_create_date(self) -> None:
        class _Repo:
            async def search_columns(self, embedding, *, table_ids, per_table_limit):
                return {
                    1: [
                        {
                            "table_id": 1,
                            "name": "CRDT",
                            "dtype": "character varying",
                            "score": 0.95,
                            "description": "생성일시",
                            "metadata": {"format_pattern": "YYYYMMDD"},
                        },
                        {
                            "table_id": 1,
                            "name": "LOG_TIME",
                            "dtype": "character",
                            "score": 0.4,
                            "description": "관측 시각",
                            "metadata": {"format_pattern": "YYYYMMDD"},
                        },
                    ]
                }

        planned, unresolved = await _resolve_filters(
            repository=_Repo(),
            requirements=[
                FilterRequirement(meaning="측정 기간", value_text="2025년")
            ],
            embeddings={"filter:0": [0.1, 0.2]},
            table_ids=[1],
            tables_by_id={
                1: {"schema_name": "SCHEMA_A", "original_name": "TABLE_A"}
            },
            mappings=[],
            minimum_similarity=0.2,
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(planned[0].column, "SCHEMA_A.TABLE_A.LOG_TIME")
        self.assertEqual(planned[0].value, "20250101,20251231")

    async def test_same_mention_cluster_binds_in(self) -> None:
        planned, unresolved = await _resolve_filters(
            repository=None,
            requirements=[
                FilterRequirement(meaning="취수장", value_text="팔당")
            ],
            embeddings={},
            table_ids=[1],
            tables_by_id={
                1: {"schema_name": "RWIS", "original_name": "RDISAUP_TB"}
            },
            mappings=[
                {
                    "natural_value": "팔당1취수장",
                    "code_value": "211",
                    "column_fqn": "rwis.RWIS.RDISAUP_TB.SUJ_CODE",
                    "matched_mention": "팔당",
                },
                {
                    "natural_value": "팔당2취수장",
                    "code_value": "212",
                    "column_fqn": "rwis.RWIS.RDISAUP_TB.SUJ_CODE",
                    "matched_mention": "팔당",
                },
            ],
            minimum_similarity=0.2,
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(planned[0].operator, "IN")
        self.assertEqual(planned[0].value, "211,212")
        self.assertEqual(planned[0].column, "RWIS.RDISAUP_TB.SUJ_CODE")

    async def test_numeric_measure_on_code_column_stays_unresolved(self) -> None:
        planned, unresolved = await _resolve_filters(
            repository=_ColumnSearchRepo(
                {
                    "table_id": 1,
                    "name": "SUJ_CODE",
                    "dtype": "character",
                    "score": 0.9,
                    "description": "",
                }
            ),
            requirements=[
                FilterRequirement(meaning="월 평균 ph", value_text="0")
            ],
            embeddings={"filter:0": [0.1, 0.2]},
            table_ids=[1],
            tables_by_id={
                1: {"schema_name": "SCHEMA_A", "original_name": "TABLE_A"}
            },
            mappings=[],
            minimum_similarity=0.2,
        )
        self.assertEqual(planned[0].resolution_status, "unresolved")
        self.assertTrue(unresolved)

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
