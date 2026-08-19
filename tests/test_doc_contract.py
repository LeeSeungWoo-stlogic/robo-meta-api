"""Documented T2SqlRequest fields and fail codes must match schema/engine."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from typing import get_args

from app.schemas import SqlReasonCode, T2SqlRequest, T2SqlResponse

DOC = ROOT / "docs" / "T2SQL_자연어부터_동작로직.md"
_FIELD = re.compile(r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*\|")
_SKIP_FIELDS = frozenset({"필드", "true", "false", "null"})
_CODE = re.compile(r"`(NO_METADATA|PLAN_INCOMPLETE|ENTITY_UNRESOLVED|CROSS_DB|GUARD_REJECTED|GENERATION_FAILED|TIMEOUT|UPSTREAM_UNAVAILABLE|NO_CANDIDATES)`")


def _section(text: str, heading: str, next_heading: str) -> str:
    start = text.find(heading)
    if start < 0:
        raise AssertionError(f"missing heading {heading}")
    rest = text[start + len(heading) :]
    end = rest.find(next_heading)
    if end < 0:
        return rest
    return rest[:end]


class DocContractTests(unittest.TestCase):
    def test_documented_request_fields_exist_on_schema(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        section = _section(text, "### 1.1 T2SqlRequest", "### 1.2")
        fields = [
            match.group(1)
            for line in section.splitlines()
            for match in [_FIELD.match(line)]
            if match and match.group(1) not in _SKIP_FIELDS
        ]
        self.assertIn("query", fields)
        schema_fields = set(T2SqlRequest.model_json_schema()["properties"])
        missing = [name for name in fields if name not in schema_fields]
        self.assertEqual(
            missing,
            [],
            "docs/T2SQL_자연어부터_동작로직.md Request 필드가 스키마에 없음",
        )
        self.assertNotIn("column_top_m", fields)
        self.assertNotIn("column_top_m", schema_fields)

    def test_documented_fail_codes_are_schema_literals(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        gate = _section(text, "## 8. 생성 전 게이트", "## 9.")
        response = _section(text, "## 12. 응답", "## 순서")
        codes = set(_CODE.findall(gate) + _CODE.findall(response))
        self.assertTrue(codes)
        allowed = set(get_args(SqlReasonCode))
        unknown = sorted(codes - allowed)
        self.assertEqual(unknown, [])
        response_props = T2SqlResponse.model_json_schema()["properties"]
        self.assertIn("sql_reason_code", response_props)


if __name__ == "__main__":
    unittest.main()
