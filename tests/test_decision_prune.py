"""decision 후보 score-gap / join-expand 단위 테스트."""
from __future__ import annotations

import unittest

from app.services.decision_service import (
    _prune_by_score_gap,
    _select_final_candidates,
    _table_description_text,
)
from app.services.neo4j_client.models import TableCandidate


class PruneByScoreGapTest(unittest.TestCase):
    def test_single_clear_winner(self) -> None:
        rows = [
            TableCandidate("rwis", "A", "", "", score=0.9),
            TableCandidate("rwis", "B", "", "", score=0.5),
            TableCandidate("rwis", "C", "", "", score=0.4),
        ]
        out = _prune_by_score_gap(rows, max_k=10, gap_ratio=0.85)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].name, "A")

    def test_multiple_within_gap(self) -> None:
        rows = [
            TableCandidate("rwis", "A", "", "", score=1.0),
            TableCandidate("rwis", "B", "", "", score=0.9),
            TableCandidate("rwis", "C", "", "", score=0.5),
        ]
        out = _prune_by_score_gap(rows, max_k=10, gap_ratio=0.85)
        self.assertEqual([t.name for t in out], ["A", "B"])

    def test_min_step_cuts_cluster(self) -> None:
        rows = [
            TableCandidate("rwis", "A", "", "", score=0.738),
            TableCandidate("rwis", "B", "", "", score=0.734),
            TableCandidate("rwis", "C", "", "", score=0.717),
        ]
        out = _prune_by_score_gap(rows, max_k=10, gap_ratio=0.85, min_step=0.012)
        self.assertEqual([t.name for t in out], ["A", "B"])


class JoinExpandTest(unittest.TestCase):
    def test_no_bridge_returns_pruned(self) -> None:
        pool = [
            TableCandidate("rwis", "A", "", "", score=0.9),
            TableCandidate("rwis", "B", "", "", score=0.5),
        ]
        pruned = [pool[0]]
        out = _select_final_candidates(
            pool,
            pruned,
            bridges_all=[],
            join_expand=True,
            expand_via=("fk",),
            max_k=10,
            gap_ratio=0.85,
            min_step=0.0,
            top_radius=0.01,
        )
        self.assertEqual(len(out), 1)

    def test_fk_one_hop_from_anchor_only(self) -> None:
        pool = [
            TableCandidate("rwis", "A", "", "", score=0.9),
            TableCandidate("rwis", "B", "", "", score=0.5),
            TableCandidate("rwis", "C", "", "", score=0.4),
        ]
        pruned = [pool[0]]
        bridges = [{
            "from_key": "rwis.a",
            "to_key": "rwis.b",
            "bridge": type("B", (), {"via": "fk"})(),
        }]
        out = _select_final_candidates(
            pool,
            pruned,
            bridges_all=bridges,
            join_expand=True,
            expand_via=("fk",),
            max_k=10,
            gap_ratio=0.85,
            min_step=0.0,
            top_radius=0.01,
        )
        self.assertEqual([t.name for t in out], ["A"])

    def test_convention_does_not_expand(self) -> None:
        pool = [
            TableCandidate("rwis", "A", "", "", score=0.9),
            TableCandidate("rwis", "LOW", "", "", score=0.43),
        ]
        pruned = [pool[0]]
        bridges = [{
            "from_key": "rwis.a",
            "to_key": "rwis.low",
            "bridge": type("B", (), {"via": "convention"})(),
        }]
        out = _select_final_candidates(
            pool,
            pruned,
            bridges_all=bridges,
            join_expand=True,
            expand_via=("fk",),
            max_k=10,
            gap_ratio=0.85,
            min_step=0.0,
            top_radius=0.01,
        )
        self.assertEqual([t.name for t in out], ["A"])


class TableDescriptionTest(unittest.TestCase):
    def test_analyzed_preferred(self) -> None:
        t = TableCandidate("rwis", "T", "raw", "analyzed", score=1.0)
        comment, desc = _table_description_text(t)
        self.assertEqual(comment, "raw")
        self.assertEqual(desc, "analyzed")


class MartConventionExcludeTest(unittest.TestCase):
    def test_mart_pair_detection(self) -> None:
        from app.services.neo4j_client.vector_search import _is_mart_fact_table

        self.assertTrue(_is_mart_fact_table("fct_measure_hour"))
        self.assertTrue(_is_mart_fact_table("FCT_TAG_SUNSI"))
        self.assertFalse(_is_mart_fact_table("RDR01MI_HQJBDB"))
        self.assertFalse(_is_mart_fact_table("RDISAUP_TB"))

    def test_mart_mart_rows_filtered(self) -> None:
        rows = [
            {
                "from_table": "fct_measure_day",
                "to_table": "fct_measure_hour",
                "join_column": "tagsn",
            },
            {
                "from_table": "fct_measure_day",
                "to_table": "RDR01MI_HQJBDB",
                "join_column": "tagsn",
            },
        ]
        filtered = [
            r
            for r in rows
            if not (
                r["from_table"].lower().startswith("fct_")
                and r["to_table"].lower().startswith("fct_")
            )
        ]
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["to_table"], "RDR01MI_HQJBDB")


if __name__ == "__main__":
    unittest.main()
