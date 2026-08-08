"""decision policy override 단위 테스트."""
from __future__ import annotations

import unittest

from app.services.legacy.decision_service import _resolve_decision_policy


class DecisionPolicyTest(unittest.TestCase):
    def test_table_limit_unifies_topk_and_cap(self) -> None:
        p = _resolve_decision_policy(table_limit=5)
        self.assertEqual(p["topk"], 5)
        self.assertEqual(p["table_max"], 5)
        self.assertEqual(p["table_limit"], 5)

    def test_unset_uses_env_split(self) -> None:
        p = _resolve_decision_policy()
        self.assertIsNone(p["table_limit"])
        self.assertGreaterEqual(p["topk"], 1)
        self.assertGreaterEqual(p["table_max"], 0)

    def test_clamps_bounds(self) -> None:
        p = _resolve_decision_policy(table_limit=999)
        self.assertEqual(p["topk"], 50)
        self.assertEqual(p["table_max"], 50)


if __name__ == "__main__":
    unittest.main()
