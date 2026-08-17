"""1A — robo-meta-api auth_context가 semantic-hub와 동일 auth contract를 통과한다.

fixture SoT: semantic-hub/tests/fixtures/auth_contract/ (공유).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import jwt as pyjwt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.security.auth_context import (
    AuthConfig, AuthContext, AuthError, require_tenant, verify_token,
)

FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "semantic-hub" / "tests" / "fixtures" / "auth_contract"
)
CONTRACT = json.loads((FIXTURE_DIR / "scenarios.json").read_text(encoding="utf-8"))


def _config() -> AuthConfig:
    cfg = CONTRACT["config"]
    return AuthConfig.from_jwks_file(
        FIXTURE_DIR / "jwks.json", issuer=cfg["issuer"], audience=cfg["audience"])


def build_token(scenario: dict) -> str:
    cfg = CONTRACT["config"]
    now = int(time.time())
    claims = dict(CONTRACT["base_claims"])
    claims.update(scenario.get("overrides", {}))
    exp_offset = claims.pop("exp_offset_s")
    nbf_offset = claims.pop("nbf_offset_s")
    payload = {
        "iss": claims.pop("iss", cfg["issuer"]),
        "aud": claims.pop("aud", cfg["audience"]),
        "exp": now + exp_offset,
        "nbf": now + nbf_offset,
        "iat": now,
        **{k: v for k, v in claims.items() if v is not None},
    }
    for k, v in scenario.get("overrides", {}).items():
        if v is None:
            payload.pop(k, None)

    sign_key = scenario["sign_key"]
    kid = scenario.get("sign_kid", cfg["kid"])
    if sign_key == "none":
        return pyjwt.encode(payload, key=None, algorithm="none", headers={"kid": kid})
    pem = (FIXTURE_DIR / f"{sign_key}_private.pem").read_bytes()
    return pyjwt.encode(payload, key=pem, algorithm=cfg["algorithm"], headers={"kid": kid})


@pytest.mark.parametrize("scenario", CONTRACT["scenarios"], ids=lambda s: s["name"])
def test_auth_contract_scenarios(scenario):
    token = build_token(scenario)
    config = _config()
    if scenario["expect"] == "ok":
        ctx = verify_token(token, config)
        assert ctx.subject == scenario["expect_subject"]
        assert ctx.tenant_id == scenario["expect_tenant"]
        assert ctx.roles == frozenset(scenario["expect_roles"])
    else:
        with pytest.raises(AuthError) as e:
            verify_token(token, config)
        assert e.value.code == scenario["expect_code"], scenario["name"]


def test_cross_tenant_denied():
    ctx = AuthContext(subject="u1", tenant_id="kwater", roles=frozenset({"CONSUMER"}))
    require_tenant(ctx, "kwater")
    with pytest.raises(AuthError) as e:
        require_tenant(ctx, "other-tenant")
    assert e.value.code == "AUTH_TENANT_DENIED"
