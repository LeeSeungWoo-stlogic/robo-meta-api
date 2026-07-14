"""5C 완료 게이트 — `/v2/data_decision` Bundle 공급.

- Bundle이 metadata-context-bundle-v2.schema.json(독립 consumer contract)을 통과
- expired/stale/unauthorized/not-ready Artifact 검색 차단
- provider 장애 시 zero vector·lexical-only 강등 없이 fail-closed
- v1 /data_decision 0.7 계약 무영향 (별도 회귀는 semantic-hub P0 시험)
"""
from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.routers import decision_v2
from app.security.auth_context import AuthConfig
from app.services.embedding_provider import (
    FailingEmbeddingProvider, FixtureEmbeddingProvider,
)
from app.services.v2_store import InMemoryV2Store

WORKSPACE = Path(__file__).resolve().parents[2]
GOLDEN_DIR = WORKSPACE / "semantic-hub" / "semantic_view" / "fixtures" / "rwis-golden"
SCHEMA_PATH = (WORKSPACE / "semantic-hub" / "semantic_view" / "schemas"
               / "metadata-context-bundle-v2.schema.json")
AUTH_DIR = WORKSPACE / "semantic-hub" / "tests" / "fixtures" / "auth_contract"
AUTH_CONTRACT = json.loads((AUTH_DIR / "scenarios.json").read_text(encoding="utf-8"))

BASELINE_QUESTION = "2025년 9월 1일 낙동강에서 강우량이 가장 많은 곳은?"
NOW = "2026-07-13T02:00:00Z"

EXECUTION_CONTEXT = {
    "backend": "mindsdb",
    "dialect": "postgresql",
    "catalog": "RWIS",
    "schema_name": "rwis",
    "identifier_quote": '"',
    "qualification_pattern": "{catalog}.{schema}.{table}",
}


def _golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def _artifact_record() -> dict:
    """publisher 저장 형태의 Artifact record."""
    artifact = _golden("expected-artifact.json")
    return {
        "artifact_id": artifact["artifact_id"],
        "tenant_id": artifact["tenant_id"],
        "view_id": artifact["view_id"],
        "view_version": artifact["view_version"],
        "status": "PUBLISHED",
        "snapshot_id": artifact["inputs"]["snapshot_id"],
        "payload": artifact["payload"],
        "payload_sha256": artifact["payload_sha256"],
        "inputs": artifact["inputs"],
        "readiness": artifact["readiness"],
        "valid_from": "2026-07-13T00:40:00Z",
        "valid_to": None,
        "published_by": "publisher-c",
    }


def _glossary() -> dict:
    bundle = _golden("expected-bundle.json")
    return bundle["glossary"]


def _embeddings() -> dict:
    question = _golden("question.json")
    return {
        "art-rwis-rainfall-day-0001": [{
            "question_text": question["question"],
            "question_sha256": question["embedding_text_sha256"],
            "embedding_model": "fixture-sha256-v1",
            "embedding_dimensions": question["embedding_dim"],
            "embedding": question["embedding"],
        }]
    }


def _token(subject="consumer-1", tenant="kwater", roles=("CONSUMER",)) -> str:
    cfg = AUTH_CONTRACT["config"]
    now = int(time.time())
    pem = (AUTH_DIR / "trusted_private.pem").read_bytes()
    return pyjwt.encode(
        {"iss": cfg["issuer"], "aud": cfg["audience"], "exp": now + 3600,
         "nbf": now - 10, "iat": now, "sub": subject, "tenant_id": tenant,
         "roles": list(roles)},
        key=pem, algorithm=cfg["algorithm"], headers={"kid": cfg["kid"]})


def _make_client(*, artifacts=None, snapshots=None, provider=None,
                 auth_enabled=True) -> TestClient:
    if auth_enabled:
        cfg = AUTH_CONTRACT["config"]
        auth_config = AuthConfig.from_jwks_file(
            AUTH_DIR / "jwks.json", issuer=cfg["issuer"], audience=cfg["audience"])
    else:
        auth_config = None
    snapshot = _golden("physical-snapshot.json")
    store = InMemoryV2Store(
        artifacts=artifacts if artifacts is not None else [_artifact_record()],
        embeddings=_embeddings(),
        snapshots=snapshots if snapshots is not None
        else {snapshot["snapshot_id"]: snapshot},
        glossary=_glossary(),
    )
    app = FastAPI()
    app.include_router(decision_v2.router)
    app.state.v2_deps = decision_v2.V2Deps(
        auth_config=auth_config,
        store=store,
        embedding_provider=provider or FixtureEmbeddingProvider(
            dimensions=16, allowed_texts={BASELINE_QUESTION}),
        execution_context=EXECUTION_CONTEXT,
        clock=lambda: NOW,
    )
    return TestClient(app)


def _headers(**kwargs) -> dict:
    return {"Authorization": f"Bearer {_token(**kwargs)}"}


# ---------------------------------------------------------------------------
# 정상 경로 + consumer contract
# ---------------------------------------------------------------------------

def test_bundle_ready_and_passes_consumer_contract():
    jsonschema = pytest.importorskip("jsonschema")
    client = _make_client()
    response = client.post("/v2/data_decision",
                           json={"query": BASELINE_QUESTION}, headers=_headers())
    assert response.status_code == 200, response.text
    bundle = response.json()
    assert bundle["meta_version"] == "2"
    assert bundle["readiness"]["state"] == "ready"
    assert len(bundle["semantic_views"]) == 1
    assert bundle["semantic_views"][0]["view_id"] == "SV-RWIS-RAINFALL-DAY"
    assert any(m["mention"] == "강우량" for m in
               bundle["query_matches"]["matched_terms"])
    assert {t["table_name"] for t in bundle["schema_context"]["tables"]} == \
        {"RDD01DD_TB", "RDITAG_TB", "RDISAUP_TB"}
    assert bundle["metric_catalog"][0]["unit"] == "mm"
    assert bundle["evidence"]["artifacts"][0]["term_hits"]
    # 독립 consumer contract: 커밋된 JSON Schema로 검증
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(bundle, schema)


def test_missing_token_rejected():
    """auth_config가 설정된 배포에서는 여전히 401 (외부 공개 전환 대비)."""
    client = _make_client()
    response = client.post("/v2/data_decision", json={"query": BASELINE_QUESTION})
    assert response.status_code == 401


def test_no_auth_mode_serves_without_token():
    """폐쇄망 무인증 구성(2026-07-13 결정): 토큰 없이 정상 Bundle 반환."""
    client = _make_client(auth_enabled=False)
    response = client.post("/v2/data_decision", json={"query": BASELINE_QUESTION})
    assert response.status_code == 200, response.text
    bundle = response.json()
    assert bundle["meta_version"] == "2"
    assert bundle["readiness"]["state"] == "ready"


# ---------------------------------------------------------------------------
# hard filter 차단
# ---------------------------------------------------------------------------

def _blocked_codes(bundle: dict) -> set[str]:
    return {b["code"] for b in bundle["readiness"]["blockers"]}


def test_expired_artifact_blocked():
    record = _artifact_record()
    record["valid_to"] = "2026-07-01T00:00:00Z"  # NOW 이전에 만료
    client = _make_client(artifacts=[record])
    bundle = client.post("/v2/data_decision", json={"query": BASELINE_QUESTION},
                         headers=_headers()).json()
    assert bundle["readiness"]["state"] == "blocked"
    assert "SV_SELECT_EXPIRED" in _blocked_codes(bundle)
    assert bundle["semantic_views"] == []


def test_stale_snapshot_blocked():
    client = _make_client(snapshots={})  # Snapshot 미등록 → 호환성 실패
    bundle = client.post("/v2/data_decision", json={"query": BASELINE_QUESTION},
                         headers=_headers()).json()
    assert bundle["readiness"]["state"] == "blocked"
    assert "SV_SELECT_STALE_SNAPSHOT" in _blocked_codes(bundle)


def test_not_ready_artifact_blocked():
    record = _artifact_record()
    record["readiness"] = {"state": "blocked", "blockers": [
        {"code": "X", "message": "m", "missing_kind": "policy", "reference": None}]}
    client = _make_client(artifacts=[record])
    bundle = client.post("/v2/data_decision", json={"query": BASELINE_QUESTION},
                         headers=_headers()).json()
    assert bundle["readiness"]["state"] == "blocked"
    assert "SV_SELECT_NOT_READY" in _blocked_codes(bundle)


def test_unauthorized_role_blocked():
    client = _make_client()
    bundle = client.post("/v2/data_decision", json={"query": BASELINE_QUESTION},
                         headers=_headers(subject="editor-x",
                                          roles=("EDITOR",))).json()
    assert bundle["readiness"]["state"] == "blocked"
    assert "SV_SELECT_ROLE_DENIED" in _blocked_codes(bundle)


def test_cross_tenant_sees_nothing():
    client = _make_client()
    bundle = client.post("/v2/data_decision", json={"query": BASELINE_QUESTION},
                         headers=_headers(tenant="other-corp")).json()
    assert bundle["readiness"]["state"] == "blocked"
    assert bundle["semantic_views"] == []
    assert "SV_SELECT_NO_ARTIFACT" in _blocked_codes(bundle)


def test_inactive_artifact_blocked():
    record = _artifact_record()
    record["status"] = "INACTIVE"
    client = _make_client(artifacts=[record])
    bundle = client.post("/v2/data_decision", json={"query": BASELINE_QUESTION},
                         headers=_headers()).json()
    assert bundle["readiness"]["state"] == "blocked"
    assert "SV_SELECT_NOT_PUBLISHED" in _blocked_codes(bundle)


# ---------------------------------------------------------------------------
# fail-closed
# ---------------------------------------------------------------------------

def test_provider_failure_is_fail_closed():
    client = _make_client(provider=FailingEmbeddingProvider())
    response = client.post("/v2/data_decision", json={"query": BASELINE_QUESTION},
                           headers=_headers())
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "EMBED_PROVIDER_DOWN"


def test_unknown_question_rejected_by_fixture_provider():
    client = _make_client()
    response = client.post("/v2/data_decision", json={"query": "임의의 다른 질문"},
                           headers=_headers())
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "EMBED_TEXT_NOT_ALLOWED"


def test_vector_only_signal_not_selected():
    """표준용어·동의어 일치가 없으면 vector 유사도만으로 선택되지 않는다."""
    client = _make_client(provider=FixtureEmbeddingProvider(dimensions=16))
    bundle = client.post("/v2/data_decision",
                         json={"query": "관계없는 재무 회계 질의"},
                         headers=_headers()).json()
    assert bundle["readiness"]["state"] == "blocked"
    assert "SV_SELECT_NO_LEXICAL_SIGNAL" in _blocked_codes(bundle)
