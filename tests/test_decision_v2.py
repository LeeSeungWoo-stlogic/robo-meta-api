"""ADR-002 Wave 2 — `/semantic_decision` 410 Gone 계약."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers import decision_v2


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(decision_v2.router)
    return TestClient(app)


def test_semantic_decision_returns_410_gone():
    response = _client().post("/semantic_decision", json={"query": "강우량"})
    assert response.status_code == 410
    detail = response.json()["detail"]
    assert detail["code"] == "SEMANTIC_DECISION_GONE"
    assert "/data_decision" in detail["message"]


def test_semantic_decision_gone_constant():
    assert decision_v2.SEMANTIC_DECISION_GONE["code"] == "SEMANTIC_DECISION_GONE"
