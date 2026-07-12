"""`/v2/data_decision` 데이터 접근 계층 (플랜 5C).

- InMemoryV2Store: deterministic E2E·시험용 (네트워크·DB 0건).
- PostgresV2Store: Metadata Store(t2s_semantic_artifacts 등) 운영 구현.
  v1 metadata_repository의 기존 메서드·SQL은 건드리지 않는다.
"""
from __future__ import annotations

from typing import Any, Protocol


class V2Store(Protocol):
    async def list_published_artifacts(self, tenant_id: str) -> list[dict[str, Any]]: ...
    async def artifact_embeddings(
        self, tenant_id: str) -> dict[str, list[dict[str, Any]]]: ...
    async def snapshot_payloads(
        self, tenant_id: str, snapshot_ids: set[str]) -> dict[str, dict[str, Any]]: ...
    async def known_snapshot_ids(self, tenant_id: str) -> set[str]: ...
    async def glossary(self, tenant_id: str) -> dict[str, Any]: ...


class InMemoryV2Store:
    def __init__(
        self,
        *,
        artifacts: list[dict[str, Any]],
        embeddings: dict[str, list[dict[str, Any]]],
        snapshots: dict[str, dict[str, Any]],
        glossary: dict[str, Any],
    ) -> None:
        self._artifacts = artifacts
        self._embeddings = embeddings
        self._snapshots = snapshots
        self._glossary = glossary

    async def list_published_artifacts(self, tenant_id: str) -> list[dict[str, Any]]:
        return [a for a in self._artifacts if a.get("tenant_id") == tenant_id]

    async def artifact_embeddings(
            self, tenant_id: str) -> dict[str, list[dict[str, Any]]]:
        tenant_artifacts = {a["artifact_id"]
                            for a in await self.list_published_artifacts(tenant_id)}
        return {aid: rows for aid, rows in self._embeddings.items()
                if aid in tenant_artifacts}

    async def snapshot_payloads(
            self, tenant_id: str, snapshot_ids: set[str]) -> dict[str, dict[str, Any]]:
        return {sid: payload for sid, payload in self._snapshots.items()
                if sid in snapshot_ids
                and payload.get("tenant_id") in (tenant_id, None)}

    async def known_snapshot_ids(self, tenant_id: str) -> set[str]:
        return {sid for sid, payload in self._snapshots.items()
                if payload.get("tenant_id") in (tenant_id, None)}

    async def glossary(self, tenant_id: str) -> dict[str, Any]:
        return self._glossary


class PostgresV2Store:
    """asyncpg pool 기반 운영 구현 (Metadata Store 스키마의 0002~0005 테이블)."""

    def __init__(self, pool, schema: str) -> None:
        self._pool = pool
        self._schema = schema

    async def list_published_artifacts(self, tenant_id: str) -> list[dict[str, Any]]:
        import json as _json

        query = f"""
        SELECT artifact_id, tenant_id, view_id, view_version, status, snapshot_id,
               payload, payload_sha256, inputs, readiness,
               to_char(valid_from, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS valid_from,
               to_char(valid_to, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS valid_to,
               published_by
        FROM {self._schema}.t2s_semantic_artifacts
        WHERE tenant_id = $1 AND status = 'PUBLISHED'
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, tenant_id)
        result = []
        for row in rows:
            record = dict(row)
            for key in ("payload", "inputs", "readiness"):
                if isinstance(record.get(key), str):
                    record[key] = _json.loads(record[key])
            result.append(record)
        return result

    async def artifact_embeddings(
            self, tenant_id: str) -> dict[str, list[dict[str, Any]]]:
        import json as _json

        query = f"""
        SELECT e.artifact_id, e.question_text, e.question_sha256,
               e.embedding_model, e.embedding_dimensions, e.embedding
        FROM {self._schema}.t2s_artifact_embeddings e
        JOIN {self._schema}.t2s_semantic_artifacts a
          ON a.artifact_id = e.artifact_id
        WHERE a.tenant_id = $1
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, tenant_id)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            record = dict(row)
            if isinstance(record.get("embedding"), str):
                record["embedding"] = _json.loads(record["embedding"])
            grouped.setdefault(record.pop("artifact_id"), []).append(record)
        return grouped

    async def snapshot_payloads(
            self, tenant_id: str, snapshot_ids: set[str]) -> dict[str, dict[str, Any]]:
        import json as _json

        if not snapshot_ids:
            return {}
        query = f"""
        SELECT snapshot_id, canonical_payload
        FROM {self._schema}.t2s_physical_snapshots
        WHERE tenant_id = $1 AND snapshot_id = ANY($2::text[])
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, tenant_id, list(snapshot_ids))
        result = {}
        for row in rows:
            payload = row["canonical_payload"]
            result[row["snapshot_id"]] = (
                _json.loads(payload) if isinstance(payload, str) else payload)
        return result

    async def known_snapshot_ids(self, tenant_id: str) -> set[str]:
        query = f"""
        SELECT snapshot_id FROM {self._schema}.t2s_physical_snapshots
        WHERE tenant_id = $1
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, tenant_id)
        return {row["snapshot_id"] for row in rows}

    async def glossary(self, tenant_id: str) -> dict[str, Any]:
        terms_query = f"""
        SELECT term AS standard_term, physical_name, description AS definition,
               domain_group AS domain
        FROM {self._schema}.t2s_terms
        WHERE stable_id IS NOT NULL
        """
        synonyms_query = f"""
        SELECT s.synonym, t.term AS standard_term
        FROM {self._schema}.t2s_synonyms s
        JOIN {self._schema}.t2s_terms t ON t.id = s.term_id
        """
        async with self._pool.acquire() as connection:
            term_rows = await connection.fetch(terms_query)
            synonym_rows = await connection.fetch(synonyms_query)
        return {
            "terms": [{**dict(row), "unit": None} for row in term_rows],
            "synonyms": [dict(row) for row in synonym_rows],
        }
