from __future__ import annotations

import re
from typing import Any

from ._base import _vector_literal


class SearchMixin:
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
               NULLIF(t.metadata->>'subject_area_override', '') AS subject_area_override,
               NULLIF(t.metadata->>'subject_area', '') AS subject_area,
               d.profile_id AS source_instance_id, d.engine,
               d.mindsdb_integration, d.mindsdb_catalog,
               s.name AS source_name,
               1 - (text_to_sql_vector <=> $1::vector) AS score
        FROM t2s_tables t
        JOIN t2s_datasources d ON d.id=t.datasource_id
        LEFT JOIN kair_platform_sources s ON s.source_id = d.source_id
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
                 c.metadata,
                 EXISTS (
                   SELECT 1 FROM t2s_fk_constraints fk
                   WHERE fk.from_column_id=c.id
                 ) AS is_foreign_key,
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
               vm.verified, vm.origin, vm.metadata,
               c.name AS column_name,
               t.id AS table_id, t.db, t.schema_name, t.name AS table_name,
               d.profile_id AS source_instance_id
        FROM t2s_value_mappings vm
        LEFT JOIN t2s_columns c ON c.id=vm.column_id
        LEFT JOIN t2s_tables t ON t.id=c.table_id
        LEFT JOIN t2s_datasources d ON d.id=t.datasource_id
        WHERE vm.verified = true
          AND t.text_to_sql_is_valid=true
          AND t.review_status='approved'
          AND position(
                lower(regexp_replace(vm.natural_value, '\\s+', '', 'g'))
                in lower(regexp_replace($1, '\\s+', '', 'g'))
              ) > 0
        ORDER BY length(regexp_replace(vm.natural_value, '\\s+', '', 'g')) DESC,
                 vm.code_value
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, question)
        return [
            dict(row)
            for row in rows
            if self._natural_value_is_standalone_mention(
                question, str(row.get("natural_value") or "")
            )
        ]

    @staticmethod
    def _compact_natural_text(value: str) -> str:
        """Drop whitespace so Store labels like '탁 도' match query '탁도'."""
        return re.sub(r"\s+", "", value or "").casefold()

    @staticmethod
    def _natural_value_is_standalone_mention(question: str, natural_value: str) -> bool:
        """Reject Hangul mid-word hits (e.g. natural_value '정수' inside '정수장').

        Allow verified geo/admin compounds: '충청' inside '충청지역'.
        Matching is whitespace-insensitive ('탁 도' ↔ '탁도') via flexible
        whitespace between label characters on the original question.
        """
        if not natural_value:
            return False
        chars = list(SearchMixin._compact_natural_text(natural_value))
        if not chars:
            return False
        # Keep original spacing in the question; allow optional spaces between
        # label characters, but not after the final character (that would eat
        # the following token separator and falsely look mid-word).
        pattern = "".join(
            re.escape(ch) + (r"\s*" if index < len(chars) - 1 else "")
            for index, ch in enumerate(chars)
        )
        compound_suffixes = (
            "지역",
            "권역",
            "본부",
            "유역",
            "지구",
            "권",
            "시",
            "군",
            "도",
            "부",
        )

        def _is_hangul(ch: str) -> bool:
            return bool(ch) and "\uac00" <= ch <= "\ud7a3"

        for match in re.finditer(pattern, question, flags=re.IGNORECASE):
            before = question[match.start() - 1] if match.start() > 0 else ""
            immediate_after = (
                question[match.end()] if match.end() < len(question) else ""
            )
            # Whitespace or EOS is a token boundary (do not look past spaces into
            # the next Hangul word — that rejected '화성정수장 평균 …').
            if not _is_hangul(before) and (
                not immediate_after
                or immediate_after.isspace()
                or not _is_hangul(immediate_after)
            ):
                return True
            if not _is_hangul(before) and _is_hangul(immediate_after):
                rest = question[match.end() :]
                if any(rest.startswith(suffix) for suffix in compound_suffixes):
                    return True
        return False
