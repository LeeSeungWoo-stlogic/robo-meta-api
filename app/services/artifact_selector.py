"""질문별 published Artifact 선택 (플랜 5C).

hard filter (전부 만족해야 후보):
- tenant/role, status=PUBLISHED, 유효기간, Snapshot 호환성, readiness=ready

ranking:
- exact/synonym term > metric/dimension 명칭 > domain > 대표 질문 embedding 유사도
- vector score 단독 확정 금지: lexical(term) 신호가 0이면 선택하지 않는다.
- 적합 Artifact가 없으면 즉석 생성하지 않고 blocker를 반환한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SelectedArtifact:
    record: dict[str, Any]
    score: float
    term_hits: list[str]
    embedding_similarity: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class SelectionResult:
    selected: list[SelectedArtifact]
    blockers: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.selected)


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _lexical_terms(record: dict[str, Any],
                   glossary: dict[str, Any] | None) -> dict[str, str]:
    """Artifact가 답할 수 있는 표면형 → 근거 종류."""
    surface: dict[str, str] = {}
    payload = record.get("payload") or {}
    for binding in payload.get("bindings", []):
        concept = binding.get("concept")
        if concept and concept.get("label"):
            surface[concept["label"]] = "concept_label"
        name = binding.get("name") or ""
        if "." in name:
            surface[name.split(".", 1)[1]] = "binding_name"
    for term in (glossary or {}).get("terms", []):
        if term.get("standard_term"):
            surface[term["standard_term"]] = "standard_term"
    for synonym in (glossary or {}).get("synonyms", []):
        if synonym.get("synonym"):
            surface[synonym["synonym"]] = "synonym"
    return surface


def hard_filter(
    record: dict[str, Any],
    *,
    tenant_id: str,
    roles: frozenset[str] | set[str],
    now: str,
    known_snapshot_ids: set[str],
) -> str | None:
    """탈락 사유 코드를 반환한다. 통과 시 None."""
    if record.get("tenant_id") != tenant_id:
        return "SV_SELECT_TENANT_DENIED"
    if record.get("status") != "PUBLISHED":
        return "SV_SELECT_NOT_PUBLISHED"
    readiness = record.get("readiness") or {}
    if readiness.get("state") != "ready":
        return "SV_SELECT_NOT_READY"
    valid_from = record.get("valid_from")
    valid_to = record.get("valid_to")
    if valid_from and now < valid_from:
        return "SV_SELECT_NOT_YET_VALID"
    if valid_to and now > valid_to:
        return "SV_SELECT_EXPIRED"
    if record.get("snapshot_id") not in known_snapshot_ids:
        return "SV_SELECT_STALE_SNAPSHOT"
    security = next(
        (p for p in (record.get("payload") or {}).get("policies", [])
         if p.get("policy_type") == "SECURITY"), None)
    if security is not None:
        allowed_roles = set((security.get("rule") or {}).get("allowed_roles") or [])
        if allowed_roles and not (allowed_roles & set(roles)):
            return "SV_SELECT_ROLE_DENIED"
    return None


def select_artifacts(
    *,
    question: str,
    question_embedding: list[float],
    embedding_model: str,
    artifacts: list[dict[str, Any]],
    artifact_embeddings: dict[str, list[dict[str, Any]]],
    glossary: dict[str, Any] | None,
    tenant_id: str,
    roles: frozenset[str] | set[str],
    now: str,
    known_snapshot_ids: set[str],
    top_k: int = 3,
) -> SelectionResult:
    rejected: list[dict[str, Any]] = []
    candidates: list[SelectedArtifact] = []

    for record in artifacts:
        reason = hard_filter(record, tenant_id=tenant_id, roles=roles, now=now,
                             known_snapshot_ids=known_snapshot_ids)
        if reason is not None:
            rejected.append({"code": reason,
                             "message": f"{record.get('artifact_id')} 제외",
                             "missing_kind": "other",
                             "reference": record.get("artifact_id")})
            continue

        term_hits: list[str] = []
        weights = {"standard_term": 1.0, "synonym": 1.0,
                   "concept_label": 0.8, "binding_name": 0.6}
        term_score = 0.0
        for surface, kind in _lexical_terms(record, glossary).items():
            if surface and surface in question:
                term_hits.append(surface)
                term_score += weights.get(kind, 0.5)

        similarity = 0.0
        for row in artifact_embeddings.get(record["artifact_id"], []):
            if row.get("embedding_model") != embedding_model:
                continue
            similarity = max(similarity, _cosine(question_embedding, row["embedding"]))

        if not term_hits:
            # vector 단독 확정 금지
            rejected.append({
                "code": "SV_SELECT_NO_LEXICAL_SIGNAL",
                "message": f"{record['artifact_id']}: 표준용어·동의어 일치 없음 "
                           "(vector 단독 선택 금지)",
                "missing_kind": "binding",
                "reference": record["artifact_id"]})
            continue

        score = min(term_score, 3.0) / 3.0 * 0.6 + max(similarity, 0.0) * 0.4
        candidates.append(SelectedArtifact(
            record=record, score=round(score, 6), term_hits=term_hits,
            embedding_similarity=round(similarity, 6),
            reasons=[f"term:{t}" for t in term_hits]
                    + ([f"similarity:{round(similarity, 4)}"] if similarity else [])))

    candidates.sort(key=lambda c: (-c.score, c.record["artifact_id"]))
    if candidates:
        return SelectionResult(selected=candidates[:top_k], blockers=[])
    return SelectionResult(selected=[], blockers=rejected or [{
        "code": "SV_SELECT_NO_ARTIFACT",
        "message": "published Semantic View가 없습니다.",
        "missing_kind": "other",
        "reference": None,
    }])
