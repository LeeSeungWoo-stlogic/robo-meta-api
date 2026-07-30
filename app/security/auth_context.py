"""robo-meta-api 인증 컨텍스트 (Semantic View serving plane, 1A).

semantic-hub의 auth_context와 구현은 독립이지만 동일 auth contract fixture
(semantic-hub/tests/fixtures/auth_contract/)를 공유하며 같은 시나리오를
통과해야 한다. 검증된 `sub`, `tenant_id`, role만 권한 판단에 사용한다.
v1 `/data_decision` 경로의 기존 인증 방식은 변경하지 않는다. 이 모듈은
`/semantic_decision` 등 Semantic View 경로 전용이다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import jwt
from jwt import algorithms as jwt_algorithms

ALLOWED_ROLES: frozenset[str] = frozenset(
    {"ADMIN", "EDITOR", "REVIEWER", "PUBLISHER", "CONSUMER"}
)
ALLOWED_ALGORITHMS: tuple[str, ...] = ("RS256",)


class AuthError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class AuthConfig:
    issuer: str
    audience: str
    jwks: Mapping[str, Any]
    leeway_seconds: int = 0

    @classmethod
    def from_jwks_file(cls, path: str | Path, *, issuer: str, audience: str) -> "AuthConfig":
        jwks = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(issuer=issuer, audience=audience, jwks=jwks)


@dataclass(frozen=True)
class AuthContext:
    subject: str
    tenant_id: str
    roles: frozenset[str] = field(default_factory=frozenset)

    def has_any_role(self, *roles: str) -> bool:
        return bool(self.roles & set(roles))


def _resolve_key(config: AuthConfig, token: str):
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as e:
        raise AuthError("AUTH_TOKEN_MALFORMED", f"token header 해석 실패: {e}") from e

    alg = header.get("alg")
    if alg not in ALLOWED_ALGORITHMS:
        raise AuthError("AUTH_ALG_NOT_ALLOWED", f"허용되지 않은 algorithm: {alg!r}")

    kid = header.get("kid")
    for key in config.jwks.get("keys", []):
        if key.get("kid") == kid and key.get("kty") == "RSA":
            return jwt_algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
    raise AuthError("AUTH_KEY_UNKNOWN", f"JWKS에 없는 kid: {kid!r}")


def verify_token(token: str, config: AuthConfig) -> AuthContext:
    public_key = _resolve_key(config, token)
    try:
        claims = jwt.decode(
            token,
            key=public_key,
            algorithms=list(ALLOWED_ALGORITHMS),
            issuer=config.issuer,
            audience=config.audience,
            leeway=config.leeway_seconds,
            options={
                "require": ["exp", "iss", "aud", "sub"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
    except jwt.ExpiredSignatureError as e:
        raise AuthError("AUTH_EXPIRED", "token 만료") from e
    except jwt.ImmatureSignatureError as e:
        raise AuthError("AUTH_NOT_YET_VALID", "token이 아직 유효하지 않음(nbf)") from e
    except jwt.InvalidIssuerError as e:
        raise AuthError("AUTH_ISSUER_INVALID", "issuer 불일치") from e
    except jwt.InvalidAudienceError as e:
        raise AuthError("AUTH_AUDIENCE_INVALID", "audience 불일치") from e
    except jwt.InvalidSignatureError as e:
        raise AuthError("AUTH_SIGNATURE_INVALID", "signature 검증 실패") from e
    except jwt.MissingRequiredClaimError as e:
        raise AuthError("AUTH_CLAIMS_INVALID", f"필수 claim 누락: {e}") from e
    except jwt.InvalidTokenError as e:
        raise AuthError("AUTH_TOKEN_MALFORMED", f"token 검증 실패: {e}") from e

    subject = claims.get("sub")
    tenant_id = claims.get("tenant_id")
    raw_roles = claims.get("roles")
    if not subject or not isinstance(subject, str):
        raise AuthError("AUTH_CLAIMS_INVALID", "sub claim이 비어 있음")
    if not tenant_id or not isinstance(tenant_id, str):
        raise AuthError("AUTH_CLAIMS_INVALID", "tenant_id claim이 비어 있음")
    if not isinstance(raw_roles, list) or not raw_roles:
        raise AuthError("AUTH_CLAIMS_INVALID", "roles claim이 비어 있음")
    roles = frozenset(str(r) for r in raw_roles)
    unknown = roles - ALLOWED_ROLES
    if unknown:
        raise AuthError("AUTH_CLAIMS_INVALID", f"알 수 없는 role: {sorted(unknown)}")

    return AuthContext(subject=subject, tenant_id=tenant_id, roles=roles)


def require_roles(context: AuthContext, *roles: str) -> None:
    if not context.has_any_role(*roles):
        raise AuthError(
            "AUTH_ROLE_DENIED",
            f"필요 role {sorted(roles)} 없음 (보유: {sorted(context.roles)})",
        )


def require_tenant(context: AuthContext, tenant_id: str) -> None:
    if context.tenant_id != tenant_id:
        raise AuthError("AUTH_TENANT_DENIED", "다른 tenant 자원 접근 거부")
