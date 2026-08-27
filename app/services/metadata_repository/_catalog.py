from __future__ import annotations

from typing import Any


class CatalogMixin:
    async def list_tables(self) -> list[dict[str, Any]]:
        query = """
        SELECT db,
               schema_name,
               original_name AS table_name,
               description,
               analyzed_description,
               metadata
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

    async def fetch_serving_catalog(self) -> dict[str, Any]:
        active_query = """
        SELECT EXISTS (
          SELECT 1 FROM t2s_snapshot_activations
          WHERE sink_name = 't2s_serving'
        ) AS serving_active
        """
        rows_query = """
        SELECT s.name AS source_name,
               d.engine,
               d.source_schema,
               to_char(
                 d.created_at AT TIME ZONE 'Asia/Seoul',
                 'YYYY-MM-DD"T"HH24:MI:SS+09:00'
               ) AS registered_at,
               t.schema_name,
               COALESCE(t.original_name, t.name) AS table_name,
               t.description AS table_comment,
               t.analyzed_description AS table_description,
               t.metadata AS table_metadata,
               c.name AS column_name,
               c.dtype,
               c.metadata,
               c.nullable,
               c.is_primary_key,
               c.description AS column_comment,
               c.analyzed_description AS column_description,
               t_to.schema_name AS ref_schema_name,
               COALESCE(t_to.original_name, t_to.name) AS ref_table_name,
               c_to.name AS ref_column_name,
               fk.constraint_name AS ref_constraint_name,
               COALESCE((fk.metadata->>'position')::int, 1) AS ref_position,
               t_from.schema_name AS inbound_schema_name,
               COALESCE(t_from.original_name, t_from.name) AS inbound_table_name,
               c_from.name AS inbound_column_name,
               fk_in.constraint_name AS inbound_constraint_name,
               COALESCE((fk_in.metadata->>'position')::int, 1) AS inbound_position
        FROM t2s_datasources d
        LEFT JOIN kair_platform_sources s ON s.source_id = d.source_id
        JOIN t2s_snapshot_activations act
          ON act.source_instance_id = d.profile_id
         AND act.sink_name = 't2s_serving'
        JOIN t2s_tables t ON t.datasource_id = d.id
        JOIN t2s_snapshot_activations a
          ON a.source_instance_id = d.profile_id
         AND a.sink_name = 't2s_serving'
         AND a.snapshot_id = t.metadata->>'snapshot_id'
        JOIN t2s_columns c
          ON c.table_id = t.id
         AND c.review_status = 'approved'
        LEFT JOIN t2s_fk_constraints fk ON fk.from_column_id = c.id
        LEFT JOIN t2s_columns c_to
          ON c_to.id = fk.to_column_id
         AND c_to.review_status = 'approved'
        LEFT JOIN t2s_tables t_to ON t_to.id = c_to.table_id
        LEFT JOIN t2s_fk_constraints fk_in ON fk_in.to_column_id = c.id
        LEFT JOIN t2s_columns c_from
          ON c_from.id = fk_in.from_column_id
         AND c_from.review_status = 'approved'
        LEFT JOIN t2s_tables t_from
          ON t_from.id = c_from.table_id
         AND t_from.text_to_sql_is_valid = true
         AND t_from.review_status = 'approved'
        WHERE t.text_to_sql_is_valid = true
          AND t.review_status = 'approved'
        ORDER BY s.name, d.engine, d.source_schema,
                 t.schema_name, COALESCE(t.original_name, t.name), c.name,
                 t_to.schema_name, COALESCE(t_to.original_name, t_to.name), c_to.name,
                 t_from.schema_name, COALESCE(t_from.original_name, t_from.name),
                 c_from.name
        """
        async with self._pool.acquire() as connection:
            active = await connection.fetchrow(active_query)
            rows = await connection.fetch(rows_query)
        return {
            "serving_active": bool(active["serving_active"]) if active else False,
            "rows": [dict(row) for row in rows],
        }
