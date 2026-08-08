from __future__ import annotations

import json
from typing import Any


class ExecutionSourceMixin:
    async def list_execution_sources(self) -> list[dict[str, Any]]:
        """Return all published datasources (health / boot inventory)."""

        query = """
        SELECT d.profile_id AS source_instance_id,
               d.engine,
               d.source_schema,
               d.mindsdb_integration,
               d.mindsdb_catalog,
               s.name AS source_name
        FROM t2s_datasources d
        LEFT JOIN kair_platform_sources s ON s.source_id = d.source_id
        ORDER BY d.profile_id
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query)
        return [dict(row) for row in rows]

    async def execution_source_scope(
        self,
        source_instance_id: str,
    ) -> dict[str, Any] | None:
        """Return one datasource and its active approved execution allowlist."""

        query = """
        SELECT d.profile_id AS source_instance_id,
               d.engine,
               d.source_schema,
               d.mindsdb_integration,
               d.mindsdb_catalog,
               s.name AS source_name,
               coalesce(
                 jsonb_agg(
                   DISTINCT jsonb_build_object(
                     'schema_name', t.schema_name,
                     'original_name', t.original_name
                   )
                 ) FILTER (
                   WHERE t.original_name IS NOT NULL
                     AND t.schema_name IS NOT NULL
                     AND a.snapshot_id IS NOT NULL
                 ),
                 '[]'::jsonb
               ) AS allowed_object_refs,
               coalesce(
                 array_agg(DISTINCT t.schema_name)
                   FILTER (
                     WHERE t.schema_name IS NOT NULL
                       AND a.snapshot_id IS NOT NULL
                   ),
                 ARRAY[]::text[]
               ) AS allowed_schemas,
               coalesce(
                 array_agg(DISTINCT t.original_name)
                   FILTER (
                     WHERE t.original_name IS NOT NULL
                       AND a.snapshot_id IS NOT NULL
                   ),
                 ARRAY[]::text[]
               ) AS allowed_objects
        FROM t2s_datasources d
        LEFT JOIN kair_platform_sources s ON s.source_id = d.source_id
        LEFT JOIN t2s_tables t
          ON t.datasource_id=d.id
         AND t.text_to_sql_is_valid=true
         AND t.review_status='approved'
        LEFT JOIN t2s_snapshot_activations a
          ON a.source_instance_id=d.profile_id
         AND a.sink_name='t2s_serving'
         AND a.snapshot_id=t.metadata->>'snapshot_id'
        WHERE d.profile_id=$1
        GROUP BY d.profile_id, d.engine, d.source_schema,
                 d.mindsdb_integration, d.mindsdb_catalog, s.name
        """
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(query, source_instance_id)
        if row is None:
            return None
        payload = dict(row)
        refs = payload.get("allowed_object_refs") or []
        if isinstance(refs, str):
            refs = json.loads(refs)
        payload["allowed_object_refs"] = [
            {
                "schema_name": str(item.get("schema_name") or ""),
                "original_name": str(item.get("original_name") or ""),
            }
            for item in refs
            if isinstance(item, dict)
        ]
        return payload

    async def find_profile_ids_by_source_name(self, name: str) -> list[str]:
        """Resolve platform display name → profile_id (exact, else lower)."""

        exact = """
        SELECT d.profile_id AS source_instance_id
        FROM t2s_datasources d
        JOIN kair_platform_sources s ON s.source_id = d.source_id
        WHERE s.name = $1
        ORDER BY d.profile_id
        """
        lowered = """
        SELECT d.profile_id AS source_instance_id
        FROM t2s_datasources d
        JOIN kair_platform_sources s ON s.source_id = d.source_id
        WHERE lower(s.name) = lower($1)
        ORDER BY d.profile_id
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(exact, name)
            if not rows:
                rows = await connection.fetch(lowered, name)
        return [str(row["source_instance_id"]) for row in rows]

    async def find_profile_ids_by_mindsdb_catalog(self, catalog: str) -> list[str]:
        """Resolve mindsdb_catalog → profile_id (exact, else lower)."""

        exact = """
        SELECT d.profile_id AS source_instance_id
        FROM t2s_datasources d
        WHERE d.mindsdb_catalog = $1
        ORDER BY d.profile_id
        """
        lowered = """
        SELECT d.profile_id AS source_instance_id
        FROM t2s_datasources d
        WHERE lower(d.mindsdb_catalog) = lower($1)
        ORDER BY d.profile_id
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(exact, catalog)
            if not rows:
                rows = await connection.fetch(lowered, catalog)
        return [str(row["source_instance_id"]) for row in rows]
