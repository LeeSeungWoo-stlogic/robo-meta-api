"""`/v2/data_decision` — Semantic View Metadata Context Bundle 공급 (플랜 5C).

- v1 `/data_decision` 0.7 계약과 완전히 분리된 별도 router다.
- 인증: Bearer JWT (app/security/auth_context). tenant는 token에서만 온다.
- provider 장애 시 zero vector·lexical-only로 강등하지 않고 fail-closed(503).
- 배선(V2Deps)은 app.state.v2_deps로 주입한다. 미구성 시 503.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..security.auth_context import AuthConfig, AuthError, verify_token
from ..services.artifact_selector import select_artifacts
from ..services.bundle_assembler import assemble_bundle
from ..services.embedding_provider import EmbeddingProvider, EmbeddingProviderError
from ..services.v2_store import V2Store

router = APIRouter(tags=["decision-v2"])


@dataclass
class V2Deps:
    auth_config: AuthConfig
    store: V2Store
    embedding_provider: EmbeddingProvider
    execution_context: dict[str, Any] | None = None
    clock: Any = field(default=None)  # () -> ISO8601 str, 시험용 고정 가능


class DecisionV2Request(BaseModel):
    query: str = Field(..., min_length=1)


def _now_of(deps: V2Deps) -> str:
    if deps.clock is not None:
        return deps.clock()
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_deps(request: Request) -> V2Deps:
    deps = getattr(request.app.state, "v2_deps", None)
    if deps is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "V2_NOT_CONFIGURED",
                    "message": "/v2/data_decision 배선이 구성되지 않았습니다."})
    return deps


def _get_auth(request: Request, deps: V2Deps):
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401,
                            detail={"code": "AUTH_MISSING",
                                    "message": "Bearer token 필요"})
    try:
        return verify_token(header[7:], deps.auth_config)
    except AuthError as e:
        raise HTTPException(status_code=401,
                            detail={"code": e.code, "message": str(e)})


@router.post("/v2/data_decision")
async def data_decision_v2(request: Request, body: DecisionV2Request) -> dict[str, Any]:
    deps = _get_deps(request)
    auth = _get_auth(request, deps)

    try:
        question_embedding = await deps.embedding_provider.embed(body.query)
    except EmbeddingProviderError as e:
        # fail-closed: 강등 없이 오류 반환
        raise HTTPException(status_code=503,
                            detail={"code": e.code, "message": str(e)})

    artifacts = await deps.store.list_published_artifacts(auth.tenant_id)
    embeddings = await deps.store.artifact_embeddings(auth.tenant_id)
    known_snapshots = await deps.store.known_snapshot_ids(auth.tenant_id)
    glossary = await deps.store.glossary(auth.tenant_id)

    selection = select_artifacts(
        question=body.query,
        question_embedding=question_embedding,
        embedding_model=deps.embedding_provider.model,
        artifacts=artifacts,
        artifact_embeddings=embeddings,
        glossary=glossary,
        tenant_id=auth.tenant_id,
        roles=auth.roles,
        now=_now_of(deps),
        known_snapshot_ids=known_snapshots,
    )
    snapshot_ids = {s.record["snapshot_id"] for s in selection.selected}
    snapshots = await deps.store.snapshot_payloads(auth.tenant_id, snapshot_ids)

    return assemble_bundle(
        question=body.query,
        selection=selection,
        snapshots=snapshots,
        glossary=glossary,
        execution_context=deps.execution_context,
    )
