from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.decision_postgres.aliases import (
    TYPE_GROUPS,
    mapping_is_hq,
    mapping_is_plant,
    peel_type_suffix,
    range_surface_has_instance,
    type_product_surfaces,
    unique_code_rows,
)
from app.services.decision_postgres.decide import project_range_mappings
from app.services.decision_postgres.store_first import (
    RANGE_CODE_UNRESOLVED,
    mapping_filters,
)
from app.schemas import QueryAnalysis


class RecordingRepository:
    def __init__(self, responses: list[list[dict]]) -> None:
        self.calls: list[dict] = []
        self._responses = list(responses)

    async def find_value_mappings(
        self,
        needles=None,
        source_instance_id=None,
        extra_mentions=None,
        trusted_mentions=None,
    ):
        self.calls.append(
            {
                "needles": list(needles or []),
                "extra_mentions": extra_mentions,
                "trusted_mentions": trusted_mentions,
                "source_instance_id": source_instance_id,
            }
        )
        if self._responses:
            return self._responses.pop(0)
        return []


def _hq_row(code: str, natural: str) -> dict:
    return {
        "natural_value": natural,
        "code_value": code,
        "column_fqn": "S.HQ.BNB_CODE",
        "logical_name": "지역본부",
        "original_name": "rdibonbu_tb",
        "name": "rdibonbu_tb",
    }


def _plant_row(code: str, natural: str) -> dict:
    return {
        "natural_value": natural,
        "code_value": code,
        "column_fqn": "S.PLANT.SUJ_CODE",
        "logical_name": "사업장",
        "original_name": "rdisujang_tb",
        "name": "rdisujang_tb",
    }


class TypeGroupProtocolTests(unittest.TestCase):
    def test_longest_suffix_prefers_유역본부_over_유역(self) -> None:
        peeled = peel_type_suffix("금강유역본부")
        self.assertIsNotNone(peeled)
        instance, group = peeled
        self.assertEqual(instance, "금강")
        self.assertEqual(group.name, "hq")
        hq = next(item for item in TYPE_GROUPS if item.name == "hq")
        self.assertGreater(len(hq.suffixes[0]), len(hq.suffixes[-1]))

    def test_type_only_slot_has_no_instance(self) -> None:
        self.assertFalse(range_surface_has_instance("권역"))
        self.assertFalse(range_surface_has_instance("정수장"))
        self.assertTrue(range_surface_has_instance("금강권역"))
        self.assertTrue(range_surface_has_instance("금강"))

    def test_truncated_type_is_not_peeled(self) -> None:
        self.assertIsNone(peel_type_suffix("금강본"))

    def test_product_surfaces_are_instance_times_closed_suffixes(self) -> None:
        peeled = peel_type_suffix("금강권역")
        self.assertIsNotNone(peeled)
        instance, group = peeled
        surfaces = set(type_product_surfaces(instance, group))
        self.assertEqual(
            surfaces,
            {
                "금강지역본부",
                "금강유역본부",
                "금강권역본부",
                "금강유역",
                "금강권역",
            },
        )

    def test_dictionary_keeps_hq_and_drops_plant(self) -> None:
        hq = next(item for item in TYPE_GROUPS if item.name == "hq")
        self.assertTrue(hq.row_in_dictionary(_hq_row("701", "금강유역본부")))
        self.assertFalse(hq.row_in_dictionary(_plant_row("701", "금강남부권지역본부")))
        self.assertTrue(mapping_is_hq(_hq_row("701", "금강유역본부")))
        self.assertTrue(mapping_is_plant(_plant_row("354", "아산정수장")))

    def test_dictionary_uses_mapping_column_not_tag_table_name(self) -> None:
        hq = next(item for item in TYPE_GROUPS if item.name == "hq")
        tag_bnb = {
            "logical_name": "태그 마스터",
            "original_name": "vw_tag_dim",
            "name": "vw_tag_dim",
            "column_fqn": "rwis_mart.vw_tag_dim.bnb_code",
            "column_name": "bnb_code",
            "code_value": "701",
            "natural_value": "금강유역본부",
        }
        tag_suj = {
            "logical_name": "태그 마스터",
            "original_name": "vw_tag_dim",
            "name": "vw_tag_dim",
            "column_fqn": "rwis_mart.vw_tag_dim.suj_code",
            "column_name": "suj_code",
            "code_value": "380",
            "natural_value": "충주정수장",
        }
        self.assertTrue(mapping_is_hq(tag_bnb))
        self.assertTrue(hq.row_in_dictionary(tag_bnb))
        self.assertFalse(mapping_is_hq(tag_suj))
        self.assertFalse(hq.row_in_dictionary(tag_suj))
        self.assertTrue(mapping_is_plant(tag_suj))
        self.assertFalse(mapping_is_plant(tag_bnb))

    def test_unique_code_keeps_two_labels_one_code(self) -> None:
        rows = [
            _hq_row("701", "금강유역본부"),
            _hq_row("701", "금강권역본부"),
        ]
        kept = unique_code_rows(rows)
        self.assertEqual(len(kept), 2)
        self.assertEqual({row["code_value"] for row in kept}, {"701"})

    def test_unique_code_keeps_two_codes(self) -> None:
        rows = [
            _hq_row("701", "금강유역본부"),
            _hq_row("902", "금강유역본부(충청)"),
        ]
        kept = unique_code_rows(rows)
        self.assertEqual({row["code_value"] for row in kept}, {"701", "902"})


class SuffixStoreMergeTests(unittest.TestCase):
    def test_empty_store_keeps_geumgang_region_product(self) -> None:
        from app.services.decision_postgres.suffix_store import merge_type_groups

        groups = merge_type_groups([])
        peeled = peel_type_suffix("금강권역", groups)
        self.assertIsNotNone(peeled)
        instance, group = peeled
        self.assertEqual(instance, "금강")
        self.assertEqual(group.name, "hq")
        self.assertIn("금강권역", type_product_surfaces(instance, group))

    def test_incomplete_hq_without_region_keeps_constants(self) -> None:
        from app.services.decision_postgres.suffix_store import merge_type_groups

        groups = merge_type_groups(
            [
                {"group_name": "hq", "suffix": "지역본부", "kind": "hq"},
                {"group_name": "hq", "suffix": "유역본부", "kind": "hq"},
            ]
        )
        hq = next(item for item in groups if item.name == "hq")
        self.assertIn("권역", hq.suffixes)
        self.assertIsNotNone(peel_type_suffix("금강권역", groups))

    def test_incomplete_hq_with_partial_region_keeps_constants(self) -> None:
        from app.services.decision_postgres.suffix_store import merge_type_groups

        groups = merge_type_groups(
            [
                {"group_name": "hq", "suffix": "권역", "kind": "hq"},
                {"group_name": "hq", "suffix": "권역본부", "kind": "hq"},
            ]
        )
        hq = next(item for item in groups if item.name == "hq")
        self.assertIn("유역본부", hq.suffixes)
        self.assertIsNotNone(peel_type_suffix("금강유역본부", groups))

    def test_complete_hq_replaces_only_hq_group(self) -> None:
        from app.services.decision_postgres.suffix_store import merge_type_groups

        suffixes = ["지역본부", "유역본부", "권역본부", "유역", "권역"]
        groups = merge_type_groups(
            [
                {
                    "group_name": "hq",
                    "suffix": suffix,
                    "kind": "hq",
                    "dictionary_markers": ["지역본부", "유역본부", "권역본부"],
                }
                for suffix in suffixes
            ]
        )
        hq = next(item for item in groups if item.name == "hq")
        plant = next(item for item in groups if item.name == "plant")
        self.assertEqual(set(hq.suffixes), set(suffixes))
        self.assertIn("정수장", plant.suffixes)
        peeled = peel_type_suffix("금강권역", groups)
        self.assertIsNotNone(peeled)
        instance, group = peeled
        self.assertEqual(instance, "금강")
        self.assertEqual(
            set(type_product_surfaces(instance, group)),
            {f"금강{item}" for item in suffixes},
        )
        self.assertIsNotNone(peel_type_suffix("금강유역본부", groups))


class RangeProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage2_needles_are_product_without_extras(self) -> None:
        peeled = peel_type_suffix("금강권역")
        assert peeled is not None
        instance, group = peeled
        product = type_product_surfaces(instance, group)
        repo = RecordingRepository(
            [
                [],
                [_hq_row("701", "금강유역본부(충청)")],
            ]
        )
        bound, unresolved = await project_range_mappings(repo, ["금강권역"])
        self.assertEqual(len(repo.calls), 2)
        self.assertEqual(repo.calls[0]["needles"], ["금강권역"])
        self.assertIsNone(repo.calls[0]["extra_mentions"])
        self.assertEqual(set(repo.calls[1]["needles"]), set(product))
        self.assertIsNone(repo.calls[1]["extra_mentions"])
        self.assertIsNone(repo.calls[1]["trusted_mentions"])
        self.assertFalse(unresolved)
        self.assertEqual([row["code_value"] for row in bound], ["701"])
        self.assertEqual(bound[0]["matched_mention"], "금강권역")

    async def test_shared_stem_longer_label_does_not_bind(self) -> None:
        repo = RecordingRepository(
            [
                [],
                [_plant_row("928", "금강남부권지역본부")],
            ]
        )
        bound, unresolved = await project_range_mappings(repo, ["금강권역"])
        self.assertEqual(bound, [])
        self.assertTrue(unresolved)

    async def test_plant_row_is_dropped_from_hq_dictionary(self) -> None:
        repo = RecordingRepository(
            [
                [],
                [
                    _hq_row("701", "금강유역본부"),
                    _plant_row("701", "금강남부권지역본부"),
                ],
            ]
        )
        bound, unresolved = await project_range_mappings(repo, ["금강권역"])
        self.assertFalse(unresolved)
        self.assertEqual([row["logical_name"] for row in bound], ["지역본부"])

    async def test_type_only_does_not_mark_unresolved(self) -> None:
        repo = RecordingRepository([[]])
        bound, unresolved = await project_range_mappings(repo, ["권역"])
        self.assertEqual(bound, [])
        self.assertFalse(unresolved)
        self.assertEqual(repo.calls, [])

    async def test_typeless_zero_hits_are_unresolved(self) -> None:
        repo = RecordingRepository([[]])
        bound, unresolved = await project_range_mappings(repo, ["금강"])
        self.assertEqual(bound, [])
        self.assertTrue(unresolved)
        self.assertGreaterEqual(len(repo.calls), 1)

    async def test_typeless_plant_one_code_binds(self) -> None:
        repo = RecordingRepository(
            [
                [],
                [_plant_row("380", "충주정수장")],
            ]
        )
        bound, unresolved = await project_range_mappings(repo, ["충주"])
        self.assertFalse(unresolved)
        self.assertEqual([row["code_value"] for row in bound], ["380"])
        self.assertEqual(bound[0]["matched_mention"], "충주")

    async def test_typeless_two_codes_stay_unresolved(self) -> None:
        repo = RecordingRepository(
            [
                [],
                [
                    _plant_row("380", "충주정수장"),
                    _plant_row("381", "충주사업장"),
                ],
            ]
        )
        bound, unresolved = await project_range_mappings(repo, ["충주"])
        self.assertEqual(bound, [])
        self.assertTrue(unresolved)

    async def test_two_codes_bind_all(self) -> None:
        repo = RecordingRepository(
            [
                [],
                [
                    _hq_row("701", "금강유역본부"),
                    _hq_row("902", "금강유역본부(충청)"),
                ],
            ]
        )
        bound, unresolved = await project_range_mappings(repo, ["금강권역"])
        self.assertFalse(unresolved)
        self.assertEqual({row["code_value"] for row in bound}, {"701", "902"})
        filters = mapping_filters(bound, query="금강권역 정수장 목록")
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0].operator, "IN")
        self.assertEqual(set(filters[0].value.split(",")), {"701", "902"})

    async def test_two_labels_one_code_bind(self) -> None:
        repo = RecordingRepository(
            [
                [],
                [
                    _hq_row("701", "금강유역본부"),
                    _hq_row("701", "금강권역본부"),
                ],
            ]
        )
        bound, unresolved = await project_range_mappings(repo, ["금강권역"])
        self.assertFalse(unresolved)
        self.assertEqual(len(bound), 2)
        self.assertEqual({row["matched_mention"] for row in bound}, {"금강권역"})


class RangeFilterAxisTests(unittest.TestCase):
    def test_list_range_mention_is_not_dropped_as_target_axis(self) -> None:
        analysis = QueryAnalysis(
            status="complete",
            procedure="list",
            meaning_status="complete",
            target="금강권역",
            primary_outputs=["정수장 목록"],
        )
        rows = [
            {
                **_hq_row("701", "금강유역본부(충청)"),
                "matched_mention": "금강권역",
            }
        ]
        filters = mapping_filters(
            rows,
            query="금강권역 정수장 목록",
            analysis=analysis,
        )
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0].value, "701")
        self.assertNotEqual(RANGE_CODE_UNRESOLVED, "")

    def test_exclude_codes_are_not_eq(self) -> None:
        rows = [
            {
                **_hq_row("701", "금강유역본부"),
                "matched_mention": "금강권역",
                "filter_polarity": "exclude",
            },
            {
                **_hq_row("902", "금강유역본부(충청)"),
                "matched_mention": "금강권역",
                "filter_polarity": "exclude",
            },
        ]
        filters = mapping_filters(rows, query="금강권역이 아닌 정수장")
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0].operator, "NOT_IN")
        self.assertEqual(set(filters[0].value.split(",")), {"701", "902"})


if __name__ == "__main__":
    unittest.main()
