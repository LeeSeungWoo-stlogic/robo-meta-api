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
