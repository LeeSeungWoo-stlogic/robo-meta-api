from __future__ import annotations

from typing import Any


class JoinGraphMixin:
    async def fetch_tables_by_ids(self, table_ids: set[int]) -> list[dict[str, Any]]:
        if not table_ids:
            return []
        query = """
        SELECT t.id, t.db, t.schema_name, t.name, t.original_name,
               t.description, t.analyzed_description,
               NULLIF(t.metadata->>'subject_area_override', '') AS subject_area_override,
               NULLIF(t.metadata->>'subject_area', '') AS subject_area,
               NULLIF(t.metadata->>'logical_name', '') AS logical_name,
               d.profile_id AS source_instance_id, d.engine,
               d.mindsdb_integration, d.mindsdb_catalog,
               NULLIF(d.database_name, '') AS database_name,
               s.name AS source_name
        FROM t2s_tables t
        JOIN t2s_datasources d ON d.id=t.datasource_id
        LEFT JOIN kair_platform_sources s ON s.source_id = d.source_id
        WHERE t.id = ANY($1::bigint[])
          AND t.text_to_sql_is_valid=true
          AND t.review_status='approved'
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, list(table_ids))
        return [dict(row) for row in rows]

    async def fk_neighbor_table_ids(self, table_ids: set[int]) -> set[int]:
        if not table_ids:
            return set()
        query = """
        SELECT DISTINCT
          CASE
            WHEN c_from.table_id = ANY($1::bigint[]) THEN c_to.table_id
            ELSE c_from.table_id
          END AS neighbor_table_id
        FROM t2s_fk_constraints fk
        JOIN t2s_columns c_from ON c_from.id=fk.from_column_id
        JOIN t2s_columns c_to ON c_to.id=fk.to_column_id
        JOIN t2s_tables t_from ON t_from.id=c_from.table_id
        JOIN t2s_tables t_to ON t_to.id=c_to.table_id
        WHERE (
          c_from.table_id = ANY($1::bigint[])
          OR c_to.table_id = ANY($1::bigint[])
        )
          AND t_from.datasource_id=t_to.datasource_id
          AND t_from.text_to_sql_is_valid=true
          AND t_to.text_to_sql_is_valid=true
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, list(table_ids))
        return {int(row["neighbor_table_id"]) for row in rows}

    async def fk_bridges(self, table_ids: list[int]) -> list[dict[str, Any]]:
        if len(table_ids) < 2:
            return []
        query = """
        SELECT t_from.id AS from_table_id,
               t_from.schema_name AS from_schema,
               t_from.name AS from_table,
               c_from.name AS from_column,
               t_to.id AS to_table_id,
               t_to.schema_name AS to_schema,
               t_to.name AS to_table,
               c_to.name AS to_column,
               fk.constraint_name,
               fk.metadata
        FROM t2s_fk_constraints fk
        JOIN t2s_columns c_from ON c_from.id=fk.from_column_id
        JOIN t2s_tables t_from ON t_from.id=c_from.table_id
        JOIN t2s_columns c_to ON c_to.id=fk.to_column_id
        JOIN t2s_tables t_to ON t_to.id=c_to.table_id
        WHERE t_from.id = ANY($1::bigint[])
          AND t_to.id = ANY($1::bigint[])
          AND t_from.datasource_id=t_to.datasource_id
          AND t_from.text_to_sql_is_valid=true
          AND t_to.text_to_sql_is_valid=true
        ORDER BY t_from.name, c_from.name, t_to.name
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, table_ids)
        return [dict(row) for row in rows]

    async def fetch_join_edges(
        self,
        *,
        source_instance_id: str,
    ) -> list[dict[str, Any]]:
        """Return the approved serving JOIN graph for one source instance.

        t2s_fk_constraints is populated only from approved canonical join
        artifacts, so the serving table itself is the approval boundary.
        """
        query = """
        SELECT t_from.id AS from_table_id,
               t_from.schema_name AS from_schema,
               t_from.original_name AS from_table,
               c_from.name AS from_column,
               t_to.id AS to_table_id,
               t_to.schema_name AS to_schema,
               t_to.original_name AS to_table,
               c_to.name AS to_column,
               fk.constraint_name,
               fk.metadata
        FROM t2s_fk_constraints fk
        JOIN t2s_columns c_from ON c_from.id=fk.from_column_id
        JOIN t2s_tables t_from ON t_from.id=c_from.table_id
        JOIN t2s_datasources d_from ON d_from.id=t_from.datasource_id
        JOIN t2s_columns c_to ON c_to.id=fk.to_column_id
        JOIN t2s_tables t_to ON t_to.id=c_to.table_id
        JOIN t2s_datasources d_to ON d_to.id=t_to.datasource_id
        JOIN t2s_snapshot_activations a
          ON a.source_instance_id=d_from.profile_id
         AND a.sink_name='t2s_serving'
         AND a.snapshot_id=t_from.metadata->>'snapshot_id'
         AND a.snapshot_id=t_to.metadata->>'snapshot_id'
        WHERE d_from.profile_id=$1
          AND d_to.profile_id=$1
          AND t_from.datasource_id=t_to.datasource_id
          AND t_from.text_to_sql_is_valid=true
          AND t_to.text_to_sql_is_valid=true
          AND c_from.review_status='approved'
          AND c_to.review_status='approved'
        ORDER BY t_from.id, t_to.id, c_from.name, c_to.name
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, source_instance_id)
        return [dict(row) for row in rows]

    async def convention_bridges(
        self,
        table_ids: list[int],
    ) -> list[dict[str, Any]]:
        # Canonical v1에서는 승인된 join만 t2s_fk_constraints로 투영한다.
        # repository에서 이름 규칙을 다시 추론하면 review gate를 우회하므로 금지한다.
        return []
