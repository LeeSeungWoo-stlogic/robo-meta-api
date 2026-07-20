from __future__ import annotations

from typing import Any

import asyncpg

from ..runtime_config import RoboRuntime


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(float(item)) for item in vector) + "]"


class PostgresMetadataRepository:
    def __init__(self, pool: asyncpg.Pool, runtime: RoboRuntime):
        self._pool = pool
        self._runtime = runtime

    async def search_tables(
        self,
        embedding: list[float],
        *,
        limit: int,
        source_instance_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = """
        SELECT t.id, t.db, t.schema_name, t.name, t.original_name,
               t.description, t.analyzed_description,
               d.profile_id AS source_instance_id, d.engine,
               d.mindsdb_integration, d.mindsdb_catalog,
               1 - (text_to_sql_vector <=> $1::vector) AS score
        FROM t2s_tables t
        JOIN t2s_datasources d ON d.id=t.datasource_id
        JOIN t2s_snapshot_activations a
          ON a.source_instance_id=d.profile_id
         AND a.sink_name='t2s_serving'
         AND a.snapshot_id=t.metadata->>'snapshot_id'
        WHERE t.text_to_sql_vector IS NOT NULL
          AND t.text_to_sql_is_valid = true
          AND t.review_status='approved'
          AND t.metadata->>'embedding_model'=$4
          AND (t.metadata->>'embedding_dimensions')::int=$5
          AND ($6::text IS NULL OR d.profile_id=$6)
          AND 1 - (t.text_to_sql_vector <=> $1::vector) >= $3
        ORDER BY t.text_to_sql_vector <=> $1::vector
        LIMIT $2
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                query,
                _vector_literal(embedding),
                limit,
                self._runtime.decision.minimum_similarity,
                self._runtime.embedding.model,
                self._runtime.embedding.dimensions,
                source_instance_id,
            )
        return [dict(row) for row in rows]

    async def search_columns(
        self,
        embedding: list[float],
        *,
        table_ids: list[int],
        per_table_limit: int,
    ) -> dict[int, list[dict[str, Any]]]:
        if not table_ids:
            return {}
        query = """
        WITH ranked AS (
          SELECT c.id, c.table_id, c.name, c.fqn, c.dtype, c.nullable,
                 c.description, c.analyzed_description, c.is_primary_key,
                 1 - (c.vector <=> $1::vector) AS score,
                 row_number() OVER (
                   PARTITION BY c.table_id
                   ORDER BY c.vector <=> $1::vector
                 ) AS rank_in_table
          FROM t2s_columns c
          JOIN t2s_tables t ON t.id=c.table_id
          JOIN t2s_datasources d ON d.id=t.datasource_id
          JOIN t2s_snapshot_activations a
            ON a.source_instance_id=d.profile_id
           AND a.sink_name='t2s_serving'
           AND a.snapshot_id=t.metadata->>'snapshot_id'
          WHERE c.table_id = ANY($2::bigint[])
            AND c.vector IS NOT NULL
            AND c.review_status='approved'
            AND t.text_to_sql_is_valid=true
            AND c.metadata->>'embedding_model'=$4
            AND (c.metadata->>'embedding_dimensions')::int=$5
        )
        SELECT * FROM ranked
        WHERE rank_in_table <= $3
        ORDER BY table_id, score DESC
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                query,
                _vector_literal(embedding),
                table_ids,
                per_table_limit,
                self._runtime.embedding.model,
                self._runtime.embedding.dimensions,
            )
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(int(row["table_id"]), []).append(dict(row))
        return grouped

    async def find_value_mappings(self, question: str) -> list[dict[str, Any]]:
        query = """
        SELECT vm.natural_value, vm.code_value, vm.column_fqn,
               vm.verified, vm.origin, c.name AS column_name,
               t.id AS table_id, t.db, t.schema_name, t.name AS table_name
        FROM t2s_value_mappings vm
        LEFT JOIN t2s_columns c ON c.id=vm.column_id
        LEFT JOIN t2s_tables t ON t.id=c.table_id
        WHERE vm.verified = true
          AND t.text_to_sql_is_valid=true
          AND t.review_status='approved'
          AND position(lower(vm.natural_value) in lower($1)) > 0
        ORDER BY length(vm.natural_value) DESC, vm.code_value
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, question)
        return [dict(row) for row in rows]

    async def fetch_tables_by_ids(self, table_ids: set[int]) -> list[dict[str, Any]]:
        if not table_ids:
            return []
        query = """
        SELECT t.id, t.db, t.schema_name, t.name, t.original_name,
               t.description, t.analyzed_description,
               d.profile_id AS source_instance_id, d.engine,
               d.mindsdb_integration, d.mindsdb_catalog
        FROM t2s_tables t
        JOIN t2s_datasources d ON d.id=t.datasource_id
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

    async def list_tables(self) -> list[dict[str, Any]]:
        query = """
        SELECT db, schema_name, original_name AS table_name
        FROM t2s_tables
        ORDER BY db, schema_name, original_name
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query)
        return [dict(row) for row in rows]

    async def fetch_table_detail(
        self,
        *,
        db: str | None,
        schema_name: str,
        table_name: str,
    ) -> dict[str, Any] | None:
        table_query = """
        SELECT t.*, ds.profile_id, ds.engine
        FROM t2s_tables t
        LEFT JOIN t2s_datasources ds ON ds.id=t.datasource_id
        WHERE ($1::text IS NULL OR lower(t.db)=lower($1))
          AND lower(t.schema_name)=lower($2)
          AND lower(t.original_name)=lower($3)
        ORDER BY t.id
        LIMIT 1
        """
        columns_query = """
        SELECT c.*,
               EXISTS (
                 SELECT 1 FROM t2s_fk_constraints fk
                 WHERE fk.from_column_id=c.id
               ) AS is_foreign_key
        FROM t2s_columns c
        WHERE c.table_id=$1
        ORDER BY COALESCE((c.metadata->>'ordinal_position')::int, 2147483647), c.name
        """
        async with self._pool.acquire() as connection:
            table = await connection.fetchrow(
                table_query,
                db,
                schema_name,
                table_name,
            )
            if table is None:
                return None
            columns = await connection.fetch(columns_query, int(table["id"]))
        return {
            "table": dict(table),
            "columns": [dict(column) for column in columns],
            "refs": await self.fetch_refs(
                db=str(table["db"]),
                schema_name=str(table["schema_name"]),
                table_name=str(table["original_name"]),
            ),
        }

    async def fetch_column(
        self,
        *,
        db: str | None,
        schema_name: str,
        table_name: str,
        column_name: str,
    ) -> dict[str, Any] | None:
        detail = await self.fetch_table_detail(
            db=db,
            schema_name=schema_name,
            table_name=table_name,
        )
        if detail is None:
            return None
        for column in detail["columns"]:
            if str(column["name"]).lower() == column_name.lower():
                return column
        return None

    async def fetch_refs(
        self,
        *,
        db: str | None,
        schema_name: str,
        table_name: str,
    ) -> list[dict[str, Any]]:
        query = """
        SELECT c_from.name AS column_name,
               COALESCE((fk.metadata->>'position')::int, 1) AS position,
               t_to.schema_name AS ref_schema_name,
               t_to.original_name AS ref_table_name,
               c_to.name AS ref_column_name
        FROM t2s_fk_constraints fk
        JOIN t2s_columns c_from ON c_from.id=fk.from_column_id
        JOIN t2s_tables t_from ON t_from.id=c_from.table_id
        JOIN t2s_columns c_to ON c_to.id=fk.to_column_id
        JOIN t2s_tables t_to ON t_to.id=c_to.table_id
        WHERE ($1::text IS NULL OR lower(t_from.db)=lower($1))
          AND lower(t_from.schema_name)=lower($2)
          AND lower(t_from.original_name)=lower($3)
        ORDER BY c_from.name, position
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, db, schema_name, table_name)
        return [dict(row) for row in rows]
