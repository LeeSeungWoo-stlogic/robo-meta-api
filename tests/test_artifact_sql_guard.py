"""6단계 — robo-meta-api artifact SQL guard가 semantic-hub와 동일 계약을 지킨다.

공유 fixture: semantic-hub/semantic_view/fixtures/rwis-golden/sql-guard-cases.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.artifact_sql_guard import (
    ArtifactGuardError, enforce, validate_sql_against_artifact,
)

GOLDEN_DIR = (Path(__file__).resolve().parents[2] / "semantic-hub"
              / "semantic_view" / "fixtures" / "rwis-golden")
CONTRACT = json.loads(
    (GOLDEN_DIR / "sql-guard-cases.json").read_text(encoding="utf-8"))
ARTIFACT_PAYLOAD = json.loads(
    (GOLDEN_DIR / CONTRACT["artifact_fixture"]).read_text(encoding="utf-8"))["payload"]


@pytest.mark.parametrize("case", CONTRACT["cases"], ids=lambda c: c["name"])
def test_shared_guard_contract(case):
    errors = validate_sql_against_artifact(case["sql"], ARTIFACT_PAYLOAD)
    if case["expect_ok"]:
        assert errors == [], f"예상 밖 위반: {errors}"
    else:
        assert errors, "위반이 검출되지 않음"
        if case.get("error_contains"):
            assert any(case["error_contains"] in e for e in errors), errors


def test_enforce_raises_with_violations():
    with pytest.raises(ArtifactGuardError) as e:
        enforce('SELECT * FROM "RDF01HH_TB"', ARTIFACT_PAYLOAD)
    assert e.value.violations
