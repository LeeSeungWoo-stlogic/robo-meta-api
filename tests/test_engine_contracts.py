from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.decision_planner import CompositeJoinEdge
from app.services.decision_postgres.filters import _period_bind_value
from app.services.decision_postgres.aliases import (
    expand_region_hq_aliases,
    is_displaced_plant_mapping,
    prefer_region_hq_mappings,
)
from app.services.decision_postgres.grain import resolve_time_grain
from app.services.decision_postgres.period import (
    decision_today,
    parse_korean_period,
    parse_period_from_query,
    week_mention,
)
from app.services.decision_postgres.store_first import (
    drop_unjoinable_catalog_ids,
    assemble_anchor_join_paths,
    catalog_group_dimensions,
    location_group_tables,
    drop_unselected_fact_tables,
    facts_joinable_to_mappings,
    is_day_grain_table,
    is_month_grain_table,
    join_path_left_unresolved,
    narrow_facts_by_query_clock,
    narrow_facts_for_week,
    prefer_unique_fact_type,
    pick_fact_tables,
    resolve_time_role,
)
from app.services.t2sql.confirm import fact_left_unresolved, range_code_left_unresolved
from app.services.t2sql.llm import GENERATE_PROMPT
from app.schemas import PlannedFilter, QueryPlan
from app.services.t2sql.engine import quote_resolved_code_literals


class CatalogDropTests(unittest.TestCase):
    def test_drops_catalog_only_without_path_keeps_fact_and_mapping(self) -> None:
        edges = [
            CompositeJoinEdge(left_table_id=1, right_table_id=2, confidence=1.0),
        ]
        kept = drop_unjoinable_catalog_ids(
            {1, 2, 9},
            mapped_ids={1},
            fact_ids={2},
            catalog_ids={9, 2},
            edges=edges,
            max_hops=3,
        )
        self.assertEqual(kept, {1, 2})
        self.assertNotIn(9, kept)

    def test_drops_unmapped_catalog_when_facts_empty(self) -> None:
        kept = drop_unjoinable_catalog_ids(
            {1, 9},
            mapped_ids=set(),
            fact_ids=set(),
            catalog_ids={9},
            edges=[],
            max_hops=3,
        )
        self.assertEqual(kept, {1})

    def test_keeps_group_dimension_when_facts_empty(self) -> None:
        kept = drop_unjoinable_catalog_ids(
            {9},
            mapped_ids=set(),
            fact_ids=set(),
            catalog_ids={9},
            edges=[],
            max_hops=3,
            tables_by_id={
                9: {"id": 9, "subject_area": "master", "logical_name": "지역본부"},
            },
        )
        self.assertEqual(kept, {9})

    def test_keeps_group_dimension_without_path(self) -> None:
        kept = drop_unjoinable_catalog_ids(
            {2, 9},
            mapped_ids=set(),
            fact_ids={2},
            catalog_ids={9},
            edges=[],
            max_hops=3,
            tables_by_id={
                2: {"id": 2, "subject_area": "agg"},
                9: {"id": 9, "subject_area": "master", "logical_name": "지역본부"},
            },
        )
        self.assertEqual(kept, {2, 9})

    def test_drops_catalog_fact_when_no_chosen_fact(self) -> None:
        kept = drop_unjoinable_catalog_ids(
            {1, 9},
            mapped_ids=set(),
            fact_ids=set(),
            catalog_ids={9},
            edges=[],
            max_hops=3,
            tables_by_id={
                9: {"id": 9, "subject_area": "agg", "original_name": "RDF01HH_TB"},
            },
        )
        self.assertEqual(kept, {1})


class AnchorJoinTests(unittest.TestCase):
    def test_place_attaches_through_fact_not_parent(self) -> None:
        fact, tag, hq, plant = 10, 40, 20, 30
        edges = [
            CompositeJoinEdge(left_table_id=fact, right_table_id=tag, confidence=1.0),
            CompositeJoinEdge(left_table_id=tag, right_table_id=hq, confidence=1.0),
            CompositeJoinEdge(left_table_id=tag, right_table_id=plant, confidence=1.0),
            CompositeJoinEdge(left_table_id=hq, right_table_id=plant, confidence=1.0),
        ]
        paths, connected, unresolved = assemble_anchor_join_paths(
            {fact, hq, plant},
            edges=edges,
            max_hops=3,
            fact_ids={fact},
            mapped_ids={hq},
        )
        pairs = {
            tuple(sorted((edge.left_table_id, edge.right_table_id)))
            for path in paths
            for edge in path
        }
        self.assertEqual(unresolved, [])
        self.assertIn(tag, connected)
        self.assertIn((plant, tag), pairs)
        self.assertNotIn((hq, plant), pairs)

    def test_list_without_fact_may_use_mapping_parent(self) -> None:
        hq, plant = 20, 30
        edges = [
            CompositeJoinEdge(left_table_id=hq, right_table_id=plant, confidence=1.0),
        ]
        paths, connected, unresolved = assemble_anchor_join_paths(
            {hq, plant},
            edges=edges,
            max_hops=3,
            fact_ids=set(),
            mapped_ids={hq},
        )
        pairs = {
            tuple(sorted((edge.left_table_id, edge.right_table_id)))
            for path in paths
            for edge in path
        }
        self.assertEqual(unresolved, [])
        self.assertEqual(pairs, {(hq, plant)})
        self.assertEqual(connected, {hq, plant})

    def test_list_without_fact_does_not_pull_unselected_hop(self) -> None:
        hq, plant, extra = 20, 30, 40
        edges = [
            CompositeJoinEdge(left_table_id=hq, right_table_id=extra, confidence=1.0),
            CompositeJoinEdge(left_table_id=extra, right_table_id=plant, confidence=1.0),
        ]
        paths, connected, unresolved = assemble_anchor_join_paths(
            {hq, plant},
            edges=edges,
            max_hops=3,
            fact_ids=set(),
            mapped_ids={hq},
            blocked_ids={extra},
            allow_disconnected=True,
        )
        pairs = {
            tuple(sorted((edge.left_table_id, edge.right_table_id)))
            for path in paths
            for edge in path
        }
        self.assertEqual(unresolved, [])
        self.assertEqual(paths, [])
        self.assertEqual(pairs, set())
        self.assertEqual(connected, {hq, plant})
        self.assertNotIn(extra, connected)

    def test_list_without_fact_prefers_selected_direct_edge(self) -> None:
        hq, plant, extra = 20, 30, 40
        edges = [
            CompositeJoinEdge(left_table_id=hq, right_table_id=extra, confidence=1.0),
            CompositeJoinEdge(left_table_id=extra, right_table_id=plant, confidence=1.0),
            CompositeJoinEdge(left_table_id=hq, right_table_id=plant, confidence=0.28),
        ]
        paths, connected, unresolved = assemble_anchor_join_paths(
            {hq, plant},
            edges=edges,
            max_hops=3,
            fact_ids=set(),
            mapped_ids={hq},
            blocked_ids={extra},
            allow_disconnected=True,
        )
        pairs = {
            tuple(sorted((edge.left_table_id, edge.right_table_id)))
            for path in paths
            for edge in path
        }
        self.assertEqual(unresolved, [])
        self.assertEqual(pairs, {(hq, plant)})
        self.assertEqual(connected, {hq, plant})
        self.assertNotIn(extra, connected)


class UnselectedFactDropTests(unittest.TestCase):
    def test_strips_unchosen_fact_keeps_dimension(self) -> None:
        kept = drop_unselected_fact_tables(
            {1, 2},
            chosen_fact_ids=set(),
            tables_by_id={
                1: {"id": 1, "subject_area": "master", "original_name": "RDISAUP_TB"},
                2: {"id": 2, "subject_area": "agg", "original_name": "RDF01HH_TB"},
            },
        )
        self.assertEqual(kept, {1})

    def test_keeps_mapped_fact_seed(self) -> None:
        kept = drop_unselected_fact_tables(
            {2},
            chosen_fact_ids=set(),
            tables_by_id={
                2: {"id": 2, "subject_area": "agg", "original_name": "RDF01HH_TB"},
            },
            mapped_ids={2},
        )
        self.assertEqual(kept, {2})

    def test_keeps_chosen_fact(self) -> None:
        kept = drop_unselected_fact_tables(
            {1, 2},
            chosen_fact_ids={2},
            tables_by_id={
                1: {"id": 1, "subject_area": "master"},
                2: {"id": 2, "subject_area": "agg"},
            },
        )
        self.assertEqual(kept, {1, 2})

    def test_keeps_catalog_with_path_to_fact(self) -> None:
        edges = [
            CompositeJoinEdge(left_table_id=9, right_table_id=2, confidence=1.0),
        ]
        kept = drop_unjoinable_catalog_ids(
            {2, 9},
            mapped_ids=set(),
            fact_ids={2},
            catalog_ids={9},
            edges=edges,
            max_hops=3,
        )
        self.assertEqual(kept, {2, 9})


class FactGuessTests(unittest.TestCase):
    def test_multiple_grain_matches_are_not_silently_picked(self) -> None:
        tables = [
            {
                "id": 1,
                "logical_name": "일 DATA A",
                "subject_area": "agg",
                "description": "일별",
            },
            {
                "id": 2,
                "logical_name": "일 DATA B",
                "subject_area": "agg",
                "description": "일별",
            },
        ]
        picked, err = pick_fact_tables(tables, "day")
        self.assertEqual({int(item["id"]) for item in picked}, {1, 2})
        self.assertIsNotNone(err)
        self.assertNotEqual([int(item["id"]) for item in picked], [1])

    def test_hour_grain_falls_back_to_single_day_fact(self) -> None:
        tables = [
            {
                "id": 1,
                "logical_name": "일 DATA",
                "subject_area": "agg",
                "description": "01dd",
            },
            {
                "id": 2,
                "logical_name": "월 DATA",
                "subject_area": "agg",
                "description": "01mm",
            },
        ]
        picked, err = pick_fact_tables(tables, "hour")
        self.assertEqual([int(item["id"]) for item in picked], [1])
        self.assertIsNone(err)

    def test_unspecified_fact_defaults_to_day_not_month_or_hour(self) -> None:
        tables = [
            {
                "id": 1,
                "logical_name": "월 DATA",
                "subject_area": "agg",
                "description": "월별 01mm",
            },
            {
                "id": 2,
                "logical_name": "일 DATA",
                "subject_area": "agg",
                "description": "일별 01dd",
            },
            {
                "id": 3,
                "logical_name": "시간 DATA",
                "subject_area": "agg",
                "description": "시간별 01hh",
            },
        ]
        picked, err = pick_fact_tables(
            tables,
            None,
            query="한강유역 본부에 가장 강우량이 많았던 정수장이나 설비 알려주세요",
        )
        self.assertIsNone(err)
        self.assertEqual([int(item["id"]) for item in picked], [2])


class TimeRoleTests(unittest.TestCase):
    def test_date_and_clock_words_are_hour_grain(self) -> None:
        self.assertEqual(
            resolve_time_grain("가장 높은 곳과 해당 일자 및 시간 알려줘"),
            "hour",
        )

    def test_year_month_period_is_month_grain(self) -> None:
        self.assertEqual(
            resolve_time_grain("청주정수장 2025년 10월 한 달간 탁도 평균"),
            "month",
        )

    def test_month_window_with_event_time_is_day(self) -> None:
        self.assertEqual(
            resolve_time_grain(
                "25년 10월 한 달 동안 잔류염소가 0.2 미만으로 떨어진 사업장과 그 시점 알려줘"
            ),
            "day",
        )

    def test_month_window_drop_event_is_day(self) -> None:
        self.assertEqual(
            resolve_time_grain(
                "25년 10월 한 달 동안 잔류염소가 0.3 미만으로 떨어진 사업장들 본부별로 집계해줘"
            ),
            "day",
        )

    def test_month_window_clock_answer_is_hour(self) -> None:
        self.assertEqual(
            resolve_time_grain(
                "25년 10월에 영섬지역에서 가장 유량 높았던 지역/시간/기타 정보 알려줘"
            ),
            "hour",
        )
        self.assertEqual(
            resolve_time_grain(
                "25년 10월 한달동안 유량이 가장 많았던 시간/사업장/정보 알려줘"
            ),
            "hour",
        )

    def test_clock_narrow_keeps_hour_fact(self) -> None:
        facts = [
            {"id": 1, "logical_name": "일 DATA", "description": "01dd"},
            {"id": 2, "logical_name": "시간 DATA", "description": "01hh"},
        ]
        kept = narrow_facts_by_query_clock(facts, "일자 및 시간")
        self.assertEqual([int(item["id"]) for item in kept], [2])

    def test_prefer_unique_fact_over_raw(self) -> None:
        facts = [
            {"id": 1, "subject_area": "raw", "logical_name": "시간 RAW"},
            {"id": 2, "subject_area": "agg", "logical_name": "시간 DATA"},
        ]
        kept = prefer_unique_fact_type(facts)
        self.assertEqual([int(item["id"]) for item in kept], [2])

    def test_group_dimension_uses_mention_not_name_invention(self) -> None:
        catalog = [
            {
                "id": 3,
                "subject_area": "master",
                "logical_name": "지역본부",
                "description": "",
            },
            {
                "id": 4,
                "subject_area": "master",
                "logical_name": "한전 및 실시간인터페이스",
                "description": "",
            },
        ]
        kept = catalog_group_dimensions(catalog, ["본부별", "본부"])
        self.assertEqual([int(item["id"]) for item in kept], [3])
        code_hq = catalog_group_dimensions(
            [
                {
                    "id": 5,
                    "subject_area": "code",
                    "logical_name": "지역본부",
                    "description": "",
                }
            ],
            ["본부별", "본부"],
        )
        self.assertEqual([int(item["id"]) for item in code_hq], [5])

    def test_group_dimension_matches_plant_column_not_table_comment(self) -> None:
        catalog = [
            {
                "id": 6,
                "subject_area": "master",
                "logical_name": "태그 마스터",
                "description": "",
                "original_name": "vw_tag_dim",
                "name": "vw_tag_dim",
            }
        ]
        columns = {
            6: [
                {
                    "name": "suj_code",
                    "metadata": {"column_name_kr": "사업장코드"},
                },
                {
                    "name": "suj_name",
                    "metadata": {"column_name_kr": "사업장이름"},
                },
            ]
        }
        kept = catalog_group_dimensions(catalog, ["정수장"], columns_by_id=columns)
        self.assertEqual([int(item["id"]) for item in kept], [6])
        missed = catalog_group_dimensions(catalog, ["정수장"])
        self.assertEqual(missed, [])

    def test_location_query_keeps_store_place_masters(self) -> None:
        tables = [
            {
                "id": 7,
                "subject_area": "code",
                "logical_name": "사업장",
                "description": "",
            },
            {
                "id": 8,
                "subject_area": "code",
                "logical_name": "지역본부",
                "description": "",
            },
            {
                "id": 9,
                "subject_area": "agg",
                "logical_name": "일 DATA",
                "description": "사업장 측정",
            },
        ]
        kept = location_group_tables(
            tables,
            "2025년 9월 낙동강에서 강우량이 가장 많은 곳이 어디야?",
        )
        self.assertEqual([int(item["id"]) for item in kept], [7])
        self.assertEqual(
            location_group_tables(tables, "2025년 9월 낙동강 강우량"),
            [],
        )

    def test_list_query_time_role_is_none(self) -> None:
        self.assertEqual(resolve_time_role(procedure="list"), "none")
        self.assertEqual(resolve_time_role(procedure="lookup"), "none")

    def test_extremum_token_without_period(self) -> None:
        self.assertEqual(resolve_time_role(procedure="extremum"), "extremum")

    def test_parsed_period_is_none_role(self) -> None:
        self.assertEqual(resolve_time_role(procedure="aggregate"), "none")

    def test_period_with_peak_is_extremum_role(self) -> None:
        self.assertEqual(resolve_time_role(procedure="extremum"), "extremum")

    def test_prompt_max_only_when_latest(self) -> None:
        self.assertIn("time_role이 latest", GENERATE_PROMPT)
        self.assertIn("extremum 또는 none", GENERATE_PROMPT)
        self.assertIn("query_analysis.procedure", GENERATE_PROMPT)
        self.assertIn("answer_axis", GENERATE_PROMPT)
        self.assertIn("ORDER BY 측정컬럼", GENERATE_PROMPT)
        self.assertIn("VAL = (SELECT MAX(VAL)", GENERATE_PROMPT)
        self.assertNotIn(
            "resolved 기간 필터가 없고 선택한 fact",
            GENERATE_PROMPT,
        )
        self.assertIn("팩트 표 미선정", GENERATE_PROMPT)
        self.assertIn("alias.`*`", GENERATE_PROMPT)
        self.assertIn("IN ('981')", GENERATE_PROMPT)
        self.assertIn("목록 질의는 차원 required_tables로 SELECT", GENERATE_PROMPT)
        self.assertIn("JOIN 키만 출력하지 마라", GENERATE_PROMPT)
        self.assertIn("시계열(추이·변화·추세·트렌드)", GENERATE_PROMPT)
        self.assertIn("전일 대비 증감", GENERATE_PROMPT)
        self.assertIn("NOT_LIKE", GENERATE_PROMPT)
        self.assertIn("태그 마스터 SELECT는 측정점 식별", GENERATE_PROMPT)
        self.assertIn("answer_axis가 태그·측정점", GENERATE_PROMPT)
        self.assertIn("DISTINCT 또는 같은 축 GROUP BY", GENERATE_PROMPT)

    def test_quote_resolved_code_literals(self) -> None:
        plan = QueryPlan(
            completeness="partial",
            filters=[
                PlannedFilter(
                    meaning="코드매핑:x",
                    column="S.T.C",
                    operator="IN",
                    value="981,997",
                    resolution_status="resolved",
                )
            ],
        )
        sql = "SELECT 1 FROM T WHERE C IN (981, 997) LIMIT 20"
        out = quote_resolved_code_literals(sql, plan)
        self.assertIn("'981'", out)
        self.assertIn("'997'", out)
        self.assertNotIn("IN (981", out)
        self.assertIn("LIMIT 20", out)
        short = QueryPlan(
            completeness="partial",
            filters=[
                PlannedFilter(
                    meaning="코드매핑:x",
                    column="S.T.C",
                    operator="EQ",
                    value="1",
                    resolution_status="resolved",
                )
            ],
        )
        eq = quote_resolved_code_literals("SELECT 1 FROM T WHERE C = 1 LIMIT 1", short)
        self.assertIn("= '1'", eq)
        self.assertIn("LIMIT 1", eq)


class WeekPeriodTests(unittest.TestCase):
    def test_iso_third_week_october_2025(self) -> None:
        period = parse_korean_period("2025년 10월 셋째 주")
        self.assertIsNotNone(period)
        assert period is not None
        self.assertEqual(period.week_start, date(2025, 10, 13))
        self.assertEqual(period.week_end, date(2025, 10, 19))
        compact = _period_bind_value(
            {"dtype": "varchar", "metadata": {"format_pattern": "YYYYMMDD"}},
            period,
        )
        self.assertEqual(compact, ("BETWEEN", "20251013,20251019"))
        hourly = _period_bind_value(
            {"dtype": "varchar", "metadata": {"format_pattern": "YYYYMMDDHH"}},
            period,
        )
        self.assertEqual(hourly, ("BETWEEN", "2025101300,2025101923"))
        self.assertNotEqual(period.like_prefix, "20251015")

    def test_month_window_on_hour_clock_is_range(self) -> None:
        period = parse_korean_period("2025년 10월")
        assert period is not None
        hourly = _period_bind_value(
            {"dtype": "varchar", "metadata": {"format_pattern": "YYYYMMDDHH"}},
            period,
        )
        self.assertEqual(hourly, ("BETWEEN", "2025100100,2025103123"))
        monthly = _period_bind_value(
            {"dtype": "varchar", "metadata": {"format_pattern": "YYYYMM"}},
            period,
        )
        self.assertEqual(monthly, ("LIKE", "202510%"))

    def test_week_without_month_is_unparsed(self) -> None:
        self.assertTrue(week_mention("셋째 주 항목"))
        self.assertIsNone(parse_korean_period("셋째 주 항목"))

    def test_relative_year_and_bare_recent(self) -> None:
        today = date(2026, 8, 19)
        last = parse_korean_period("작년", today=today)
        this = parse_korean_period("올해 평균", today=today)
        self.assertIsNotNone(last)
        self.assertIsNotNone(this)
        assert last is not None and this is not None
        self.assertEqual(last.year, 2025)
        self.assertEqual(this.year, 2026)
        self.assertIsNone(parse_korean_period("최근", today=today))
        self.assertIsNone(parse_korean_period("나흘", today=today))
        self.assertIsNone(parse_korean_period("일주일", today=today))
        span = parse_korean_period("최근 3개월", today=today)
        self.assertIsNotNone(span)
        assert span is not None
        self.assertEqual(span.week_start, date(2026, 5, 19))
        self.assertEqual(span.week_end, today)
        self.assertEqual(resolve_time_role(procedure=""), "none")

    def test_korean_duration_and_position_words(self) -> None:
        today = date(2026, 8, 27)
        week = parse_korean_period("최근 일주일간 시간별 탁도 평균", today=today)
        month = parse_korean_period("최근 한 달 평균", today=today)
        three = parse_korean_period("최근 사흘", today=today)
        four = parse_korean_period("지난 나흘", today=today)
        nxt = parse_korean_period("익월 탁도", today=today)
        tomorrow = parse_korean_period("내일", today=today)
        this_week = parse_korean_period("이번주", today=today)
        self.assertIsNotNone(week)
        self.assertIsNotNone(month)
        self.assertIsNotNone(three)
        self.assertIsNotNone(four)
        self.assertIsNotNone(nxt)
        self.assertIsNotNone(tomorrow)
        self.assertIsNotNone(this_week)
        assert week is not None and month is not None
        assert three is not None and four is not None
        assert nxt is not None and tomorrow is not None
        assert this_week is not None
        self.assertEqual(week.week_start, date(2026, 8, 20))
        self.assertEqual(week.week_end, today)
        self.assertEqual(month.week_start, date(2026, 7, 27))
        self.assertEqual(three.week_start, date(2026, 8, 24))
        self.assertEqual(four.week_start, date(2026, 8, 23))
        self.assertEqual((nxt.year, nxt.month), (2026, 9))
        self.assertEqual(tomorrow.day, 28)
        self.assertEqual(this_week.week_start, date(2026, 8, 24))
        self.assertEqual(this_week.week_end, date(2026, 8, 30))
        fallback = parse_period_from_query(
            "최근 일주일간 시간별 탁도 평균",
            "최근",
            today=today,
        )
        self.assertEqual(fallback, week)
        self.assertEqual(decision_today(), decision_today())

    def test_join_disambiguates_one_fact(self) -> None:
        edges = [
            CompositeJoinEdge(left_table_id=1, right_table_id=10, confidence=1.0),
        ]
        facts = [
            {"id": 10, "logical_name": "시간 DATA A"},
            {"id": 11, "logical_name": "시간 DATA B"},
        ]
        kept = facts_joinable_to_mappings(
            facts,
            mapped_ids={1},
            edges=edges,
            max_hops=3,
        )
        self.assertEqual([int(item["id"]) for item in kept], [10])

    def test_joinable_name_hit_without_fk_keeps_fact_columns_only(self) -> None:
        facts = [
            {"id": 21, "logical_name": "분 DATA", "description": "01mi"},
            {"id": 22, "logical_name": "시간 DATA", "description": "01hh"},
            {"id": 23, "logical_name": "일 DATA", "description": "01dd"},
            {"id": 24, "logical_name": "월 DATA", "description": "01mm"},
        ]
        mappings = [
            {"column_fqn": "S.TAG_DIM.suj_code", "column_name": "suj_code", "table_id": 9},
        ]
        columns = {
            21: [{"name": "suj_code"}],
            22: [{"name": "suj_code"}],
            23: [{"name": "suj_code"}],
            24: [{"name": "suj_code"}],
            9: [{"name": "suj_code"}],
        }
        kept = facts_joinable_to_mappings(
            facts,
            mapped_ids={9},
            edges=[],
            max_hops=3,
            mappings=mappings,
            fact_columns_by_id=columns,
        )
        self.assertEqual({int(item["id"]) for item in kept}, {21, 22, 23, 24})
        dim_only = facts_joinable_to_mappings(
            facts,
            mapped_ids={9},
            edges=[],
            max_hops=3,
            mappings=mappings,
            fact_columns_by_id={9: [{"name": "suj_code"}]},
        )
        self.assertEqual(dim_only, [])

    def test_joinable_fk_fact_wins_over_name_only_mart(self) -> None:
        edges = [
            CompositeJoinEdge(left_table_id=1, right_table_id=10, confidence=1.0),
        ]
        facts = [
            {"id": 10, "logical_name": "일 DATA 원천"},
            {"id": 11, "logical_name": "일 DATA 마트", "description": "01dd"},
        ]
        mappings = [
            {"column_fqn": "S.DIM.suj_code", "column_name": "suj_code", "table_id": 1},
        ]
        kept = facts_joinable_to_mappings(
            facts,
            mapped_ids={1},
            edges=edges,
            max_hops=3,
            mappings=mappings,
            fact_columns_by_id={
                10: [{"name": "VAL"}],
                11: [{"name": "suj_code"}],
            },
        )
        self.assertEqual([int(item["id"]) for item in kept], [10])

    def test_week_without_clock_keeps_day_fact(self) -> None:
        facts = [
            {"id": 1, "logical_name": "일 DATA", "description": "01dd"},
            {"id": 2, "logical_name": "시간 DATA", "description": "01hh"},
        ]
        kept = narrow_facts_for_week(facts, "2025년 10월 셋째 주 평균")
        self.assertEqual([int(item["id"]) for item in kept], [1])
        self.assertTrue(is_day_grain_table(facts[0]))
        clock = narrow_facts_for_week(facts, "셋째 주 시간별")
        self.assertEqual({int(item["id"]) for item in clock}, {1, 2})

    def test_month_grain_exclusion_markers(self) -> None:
        self.assertTrue(
            is_month_grain_table({"logical_name": "월 DATA", "description": ""})
        )
        self.assertTrue(
            is_month_grain_table({"logical_name": "월별 집계", "description": "01mm"})
        )
        self.assertFalse(
            is_month_grain_table({"logical_name": "일 DATA", "description": "측정월 컬럼"})
        )


class RegionHqAliasTests(unittest.TestCase):
    def test_expands_yuyeok_to_region_hq_terms(self) -> None:
        extras = expand_region_hq_aliases("한강유역 사업장별", ["한강유역", "사업장"])
        self.assertIn("지역본부", extras)
        self.assertIn("권역", extras)
        self.assertIn("한강지역본부", extras)
        self.assertIn("한강유역본부", extras)

    def test_bonbu_token_opens_the_group(self) -> None:
        extras = expand_region_hq_aliases("본부별 피벗", ["본부", "피벗"])
        self.assertIn("지역본부", extras)
        self.assertIn("유역", extras)

    def test_silent_when_no_region_term(self) -> None:
        self.assertEqual(expand_region_hq_aliases("탁도 평균", ["탁도", "평균"]), [])

    def test_prefers_hq_table_over_plant_stem(self) -> None:
        rows = [
            {
                "logical_name": "지역본부",
                "code_value": "701",
                "column_fqn": "S.HQ.BNB_CODE",
                "natural_value": "금강유역본부(충청)",
            },
            {
                "logical_name": "사업장",
                "code_value": "701",
                "column_fqn": "S.PLANT.SUJ_CODE",
                "natural_value": "금강남부권지역본부",
            },
            {
                "logical_name": "별량코드 정보",
                "code_value": "CL",
                "column_fqn": "S.CODE.BR_CODE",
                "natural_value": "잔류염소",
            },
        ]
        kept = prefer_region_hq_mappings("금강권역 정수장 목록", rows)
        self.assertEqual(
            {row["column_fqn"] for row in kept},
            {"S.HQ.BNB_CODE", "S.CODE.BR_CODE"},
        )
        self.assertTrue(
            is_displaced_plant_mapping("금강권역 정수장 목록", rows[1], rows)
        )
        self.assertFalse(
            is_displaced_plant_mapping("금강권역 정수장 목록", rows[0], rows)
        )


class FactUnresolvedGateTests(unittest.TestCase):
    def test_plan_marks_fact_unresolved(self) -> None:
        self.assertTrue(
            fact_left_unresolved(
                QueryPlan(
                    completeness="partial",
                    unresolved_requirements=["팩트 표 미선정"],
                )
            )
        )
        self.assertTrue(
            fact_left_unresolved(
                QueryPlan(
                    completeness="partial",
                    unresolved_requirements=["팩트 입도를 스토어 설명과 맞출 수 없음"],
                )
            )
        )
        self.assertFalse(fact_left_unresolved(QueryPlan(completeness="complete")))
        self.assertEqual(QueryPlan(completeness="failed").time_role, "none")
        self.assertEqual(QueryPlan(completeness="failed").answer_axis, [])
        self.assertTrue(
            range_code_left_unresolved(
                QueryPlan(
                    completeness="partial",
                    unresolved_requirements=["범위 코드 미결합"],
                )
            )
        )
        self.assertFalse(range_code_left_unresolved(QueryPlan(completeness="complete")))
        self.assertTrue(
            join_path_left_unresolved(
                QueryPlan(
                    completeness="partial",
                    unresolved_requirements=["승인 JOIN 경로 없음: FACT_TB"],
                )
            )
        )
        self.assertFalse(join_path_left_unresolved(QueryPlan(completeness="complete")))


if __name__ == "__main__":
    unittest.main()
