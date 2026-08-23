"""Period grain is a query-time role, not a physical table name."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.schemas import (
    MeasurementRequirement,
    QueryAnalysis,
    SchemaRoleRequirement,
)
from app.services.decision_postgres.grain import (
    empty_result_fallback_grain,
    explicit_time_grain,
    fact_role_for_grain,
    grain_fallback_reason,
    is_measurement_role,
    is_period_fact_role,
    resolve_time_grain,
    year_window_narrows_to_month,
)
from app.services.decision_postgres.roles import (
    _enrich_analysis_roles,
    _role_candidate_score,
    prepare_role_candidate_rows,
)
from app.services.query_analysis import role_embedding_text


def _analysis(**overrides) -> QueryAnalysis:
    payload = dict(
        status="complete",
        goal="평균 탁도",
        measurement=MeasurementRequirement(metric="탁도", aggregation="AVG"),
        schema_roles=[
            SchemaRoleRequirement(
                role="정수장 명칭 마스터",
                search_terms=["정수장 명칭"],
            ),
            SchemaRoleRequirement(
                role="일별 계측 팩트",
                search_terms=["RDD01DD", "LOG_TIME"],
            ),
        ],
    )
    payload.update(overrides)
    return QueryAnalysis(**payload)


class TimeGrainTests(unittest.TestCase):
    def test_april_average_is_month_not_day(self) -> None:
        self.assertEqual(
            resolve_time_grain("25년 4월 화성정수장 평균 탁도"),
            "month",
        )

    def test_explicit_daily_wins_over_month_period(self) -> None:
        self.assertEqual(
            resolve_time_grain("4월 일별 평균 탁도"),
            "day",
        )

    def test_highest_date_wins_over_month_period(self) -> None:
        self.assertEqual(
            resolve_time_grain(
                "2025년 공주정수장 10월 TOC 농도가 가장 높은 날짜 알려줘"
            ),
            "day",
        )

    def test_explicit_monthly_still_wins_without_date_answer(self) -> None:
        self.assertEqual(
            resolve_time_grain("2025년 공주정수장의 월별 TOC 농도 평균 알려줘"),
            "month",
        )

    def test_period_month_empty_falls_back_to_day(self) -> None:
        self.assertIsNone(explicit_time_grain("단양정수장 2025년 8월 탁도 알려줘"))
        self.assertEqual(
            empty_result_fallback_grain("단양정수장 2025년 8월 탁도 알려줘"),
            "day",
        )

    def test_explicit_month_does_not_empty_fallback(self) -> None:
        self.assertEqual(
            explicit_time_grain("단양정수장 2025년 8월 월별 탁도 알려줘"),
            "month",
        )
        self.assertIsNone(
            empty_result_fallback_grain("단양정수장 2025년 8월 월별 탁도 알려줘")
        )
        self.assertIsNone(
            empty_result_fallback_grain("단양정수장 2025년 8월 월 집계 탁도 알려줘")
        )

    def test_non_month_query_does_not_empty_fallback(self) -> None:
        self.assertIsNone(empty_result_fallback_grain("화성정수장 평균 탁도"))

    def test_day_period_falls_back_to_hour_with_same_helper(self) -> None:
        self.assertEqual(
            empty_result_fallback_grain("어제 화성정수장 탁도 알려줘"),
            "hour",
        )
        self.assertEqual(
            grain_fallback_reason("month", "day"),
            "월 팩트 0건으로 일 팩트를 재조회했습니다",
        )
        self.assertEqual(
            grain_fallback_reason("day", "hour"),
            "일 팩트 0건으로 시간 팩트를 재조회했습니다",
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

    def test_month_average_place_stays_month(self) -> None:
        self.assertEqual(
            resolve_time_grain("2025년 9월 낙동강에서 강우량이 가장 많은 곳이 어디야?"),
            "month",
        )

    def test_month_window_trend_is_day(self) -> None:
        self.assertEqual(
            resolve_time_grain("청주정수장 2025년 10월 한 달간 탁도 추이 알려줘"),
            "day",
        )
        self.assertEqual(
            resolve_time_grain("충주정수장 2025년 8월 탁도변화 알려줘"),
            "day",
        )
        self.assertEqual(
            resolve_time_grain("2025년 공주정수장의 월별 TOC 농도 평균 알려줘"),
            "month",
        )

    def test_day_window_trend_is_hour(self) -> None:
        self.assertEqual(
            resolve_time_grain("어제 화성정수장 탁도 추이 알려줘"),
            "hour",
        )

    def test_year_highest_day_is_day_not_month_pool(self) -> None:
        self.assertEqual(
            resolve_time_grain(
                "2025년 아산정수장에서 수위가 가장 높은 날이 언제야?"
            ),
            "day",
        )
        self.assertFalse(year_window_narrows_to_month("day"))
        self.assertTrue(year_window_narrows_to_month(None))
        self.assertTrue(year_window_narrows_to_month("month"))

    def test_yesterday_is_day(self) -> None:
        self.assertEqual(resolve_time_grain("어제 화성정수장 평균 탁도"), "day")

    def test_fact_role_has_no_physical_table_name(self) -> None:
        role = fact_role_for_grain("month")
        blob = f"{role.role} {' '.join(role.search_terms)}".lower()
        self.assertNotIn("rdd01", blob)
        self.assertNotIn("_tb", blob)
        self.assertIn("월별", blob)
        self.assertIn("01mm", blob)

    def test_enrich_does_not_inject_domain_roles(self) -> None:
        analysis = _analysis()
        enriched = _enrich_analysis_roles("25년 4월 평균 탁도", analysis)
        self.assertEqual(
            [role.role for role in enriched.schema_roles],
            [role.role for role in analysis.schema_roles],
        )

    def test_period_fact_embedding_omits_facility_metric(self) -> None:
        analysis = _analysis()
        role = fact_role_for_grain("month")
        text = role_embedding_text(analysis, role)
        self.assertNotIn("탁도", text)
        self.assertNotIn("평균 탁도", text)
        self.assertIn("월별 계측 팩트", text)

    def test_raw_instant_table_is_dropped_for_month_role(self) -> None:
        role = fact_role_for_grain("month")
        raw = {
            "id": 1,
            "original_name": "sensor_log_shard",
            "description": "태그 식별자별 측정값과 상태를 로그 시각 기준으로 기록",
            "subject_area": "raw",
            "score": 0.9,
        }
        monthly = {
            "id": 2,
            "original_name": "metric_01mm_summary",
            "description": "월별 데이터를 월 단위 레코드로 관리하는 테이블",
            "subject_area": "agg",
            "score": 0.4,
        }
        kept = prepare_role_candidate_rows(
            role,
            [raw, monthly],
            semantic_floor=0.55,
            min_score_ratio=0.8,
        )
        names = [item["original_name"] for item in kept]
        self.assertEqual(names, ["metric_01mm_summary"])
        self.assertGreater(
            _role_candidate_score(role, monthly),
            _role_candidate_score(role, raw),
        )


if __name__ == "__main__":
    unittest.main()
