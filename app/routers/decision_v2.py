"""`/semantic_decision` — Semantic View Metadata Context Bundle 공급 (플랜 5C).

- `/data_decision` 0.7 계약과 완전히 분리된 별도 router다.
- 인증: auth_config가 설정된 경우에만 Bearer JWT를 요구한다.
  소비처가 내부 T2SQL로 한정된 폐쇄망 배포(2026-07-13 결정)에서는
  auth_config=None으로 두어 인증 없이 default_tenant_id/CONSUMER로 동작한다.
- provider 장애 시 zero vector·lexical-only로 강등하지 않고 fail-closed(503).
- 배선(V2Deps)은 app.state.v2_deps로 주입한다. 미구성 시 503.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..security.auth_context import AuthConfig, AuthContext, AuthError, verify_token
from ..services.artifact_selector import select_artifacts
from ..services.bundle_assembler import assemble_bundle
from ..services.embedding_provider import EmbeddingProvider, EmbeddingProviderError
from ..services.v2_store import V2Store

router = APIRouter(tags=["decision-v2"])


@dataclass
class V2Deps:
    auth_config: AuthConfig | None
    store: V2Store
    embedding_provider: EmbeddingProvider
    execution_context: dict[str, Any] | None = None
    clock: Any = field(default=None)  # () -> ISO8601 str, 시험용 고정 가능
    # auth_config=None(인증 비활성)일 때 사용할 고정 소비자 컨텍스트
    default_tenant_id: str = "kwater"


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
                    "message": "/semantic_decision 배선이 구성되지 않았습니다."})
    return deps


def _get_auth(request: Request, deps: V2Deps):
    if deps.auth_config is None:
        # 폐쇄망: 소비처가 내부 T2SQL로 한정되어 인증 생략 (2026-07-13 결정)
        return AuthContext(subject="internal-t2sql",
                           tenant_id=deps.default_tenant_id,
                           roles=frozenset({"CONSUMER"}))
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


@router.post("/semantic_decision")
async def semantic_decision(request: Request, body: DecisionV2Request) -> dict[str, Any]:
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
