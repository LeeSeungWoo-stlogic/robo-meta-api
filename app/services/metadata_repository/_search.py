from __future__ import annotations

import re
from typing import Any

from ._base import _vector_literal

_MENTION_TOKEN = re.compile(r"[가-힣]{2,}|[A-Za-z]{2,}")
_PARTICLE_SUFFIXES = (
    "에서부터",
    "에서",
    "에게",
    "한테",
    "부터",
    "까지",
    "처럼",
    "보다",
    "으로",
    "이나",
    "로",
    "와",
    "과",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "에",
    "만",
    "께",
    "의",
    "별",
)
# Reverse-lookup only. Full Store labels still match when they appear in the question.
_REVERSE_LOOKUP_STOPWORDS = frozenset(
    {
        "가장",
        "많은",
        "알려",
        "알려줘",
        "어디",
        "어디야",
        "평균",
        "전체",
        "얼마",
        "농도",
        "월별",
        "날짜",
        "일자",
        "시간",
        "있는",
        "항목",
        "조회",
        "측정",
        "상태",
        "목록",
        "현황",
        "최저",
        "최고",
        "최대",
        "최소",
        "최대값",
        "최소값",
        "최댓값",
        "최솟값",
        "합계",
        "총합",
        "미만",
        "이상",
        "비교",
        "기준",
        "추이",
        "사용",
        "생산",
        "수집",
        "누락",
        "대표",
        "계측",
        "데이터",
        "알려주",
        "곳이",
        "곳은",
        "year",
        "month",
        "from",
        "where",
        "select",
    }
)


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
               NULLIF(t.metadata->>'logical_name', '') AS logical_name,
               d.profile_id AS source_instance_id, d.engine,
               d.mindsdb_integration, d.mindsdb_catalog,
               NULLIF(d.database_name, '') AS database_name,
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

    @staticmethod
    def _compact_needles(values: list[str] | None) -> list[str]:
        needles: list[str] = []
        seen: set[str] = set()
        for item in values or []:
            compact = SearchMixin._compact_natural_text(item)
            if len(compact) < 2 or len(compact) > 32 or compact in seen:
                continue
            seen.add(compact)
            needles.append(compact)
        return needles

    _PLANT_MENTION_ALIASES = ("정수장", "사업장")

    @staticmethod
    def expand_plant_mention_tokens(tokens: list[str]) -> list[str]:
        compact = [SearchMixin._compact_natural_text(item) for item in tokens]
        out = list(tokens)
        if any(item in SearchMixin._PLANT_MENTION_ALIASES for item in compact):
            seen = set(compact)
            for alias in SearchMixin._PLANT_MENTION_ALIASES:
                if alias not in seen:
                    out.append(alias)
                    seen.add(alias)
        return out

    @staticmethod
    def _label_matches_needle(natural: str, needle: str) -> bool:
        compact_n = SearchMixin._compact_natural_text(needle)
        if len(compact_n) < 2:
            return False
        compact_l = SearchMixin._compact_natural_text(natural)
        head = SearchMixin._natural_label_head(natural)
        return compact_l == compact_n or head == compact_n

    async def find_value_mappings(
        self,
        needles: list[str] | None = None,
        source_instance_id: str | None = None,
        extra_mentions: list[str] | None = None,
        trusted_mentions: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        compact_needles = self._compact_needles(needles)
        extra_tokens = self._compact_needles(extra_mentions)
        if not compact_needles and not extra_tokens:
            return []
        trusted_source = (
            trusted_mentions if trusted_mentions is not None else extra_mentions
        )
        trusted_extras = {
            self._compact_natural_text(item)
            for item in (trusted_source or [])
            if self._compact_natural_text(item)
        }
        query = """
        SELECT vm.natural_value, vm.code_value, vm.column_fqn,
               vm.verified, vm.origin, vm.metadata,
               c.name AS column_name,
               t.id AS table_id, t.db, t.schema_name, t.name AS table_name,
               NULLIF(t.metadata->>'logical_name', '') AS logical_name,
               d.profile_id AS source_instance_id
        FROM t2s_value_mappings vm
        LEFT JOIN t2s_columns c ON c.id=vm.column_id
        LEFT JOIN t2s_tables t ON t.id=c.table_id
        LEFT JOIN t2s_datasources d ON d.id=t.datasource_id
        JOIN t2s_snapshot_activations a
          ON a.source_instance_id=d.profile_id
         AND a.sink_name='t2s_serving'
         AND a.snapshot_id=t.metadata->>'snapshot_id'
        WHERE vm.verified = true
          AND t.text_to_sql_is_valid=true
          AND t.review_status='approved'
          AND ($2::text IS NULL OR d.profile_id=$2)
          AND (
                EXISTS (
                  SELECT 1 FROM unnest($1::text[]) AS n(needle)
                  WHERE length(needle) >= 2
                    AND (
                      lower(regexp_replace(vm.natural_value, '\\s+', '', 'g'))
                        = needle
                      OR lower(regexp_replace(
                           split_part(split_part(vm.natural_value, '(', 1), '（', 1),
                           '\\s+', '', 'g'
                         )) = needle
                    )
                )
                OR (
                  $3::text[] IS NOT NULL
                  AND (
                    EXISTS (
                      SELECT 1 FROM unnest($3::text[]) AS tok
                      WHERE length(tok) >= 2
                        AND position(
                              tok
                              in lower(regexp_replace(vm.natural_value, '\\s+', '', 'g'))
                            ) = 1
                    )
                    OR lower(regexp_replace(COALESCE(vm.code_value, ''), '\\s+', '', 'g'))
                       = ANY($3::text[])
                  )
                )
              )
        ORDER BY length(regexp_replace(vm.natural_value, '\\s+', '', 'g')) DESC,
                 vm.code_value
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                query,
                compact_needles or None,
                source_instance_id,
                extra_tokens or None,
            )
        return self._select_mentioned_mappings(
            compact_needles,
            [dict(row) for row in rows],
            extra_tokens,
            trusted_extras=trusted_extras,
        )

    async def find_value_mapping_code_columns(
        self,
        table_ids: list[int],
    ) -> dict[int, set[str]]:
        """Approved serving value-mapping code columns, keyed by table id."""

        ids = sorted({int(item) for item in table_ids if item is not None})
        if not ids:
            return {}
        query = """
        SELECT DISTINCT c.table_id, c.name AS column_name
        FROM t2s_value_mappings vm
        JOIN t2s_columns c ON c.id = vm.column_id
        JOIN t2s_tables t ON t.id = c.table_id
        JOIN t2s_datasources d ON d.id = t.datasource_id
        JOIN t2s_snapshot_activations a
          ON a.source_instance_id = d.profile_id
         AND a.sink_name = 't2s_serving'
         AND a.snapshot_id = t.metadata->>'snapshot_id'
        WHERE vm.verified = true
          AND t.text_to_sql_is_valid = true
          AND t.review_status = 'approved'
          AND t.id = ANY($1::int[])
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, ids)
        found: dict[int, set[str]] = {}
        for row in rows:
            table_id = int(row["table_id"])
            name = str(row["column_name"] or "").strip()
            if not name:
                continue
            found.setdefault(table_id, set()).add(name)
        return found

    async def find_glossary_routes(self, needles: list[str] | None = None) -> list[dict[str, Any]]:
        """Route question surfaces to approved glossary terms and standard words.

        Surfaces include term name, korean_name, abbreviation, english_name,
        and aliases on APPROVED current standard words. Does not read the
        unofficial short-alias table.
        """
        compact_needles = self._compact_needles(needles)
        if not compact_needles:
            return []
        query = """
        SELECT standard_term, definition, surface, word_korean, surface_len
        FROM (
            SELECT t.name AS standard_term,
                   t.definition,
                   t.name AS surface,
                   NULL::text AS word_korean,
                   char_length(t.name) AS surface_len
            FROM kair_platform_active_glossary_head h
            JOIN kair_platform_glossary_version_terms m
              ON m.glossary_id = h.glossary_id
             AND m.version = h.version
            JOIN kair_platform_terms t
              ON t.term_id = m.term_id
             AND t.revision = m.term_revision
             AND t.is_current
             AND t.status = 'APPROVED'
            WHERE char_length(t.name) >= 2
              AND lower(regexp_replace(t.name, '\\s+', '', 'g')) = ANY($1::text[])
            UNION ALL
            SELECT t.name AS standard_term,
                   t.definition,
                   w.korean_name AS surface,
                   w.korean_name AS word_korean,
                   char_length(w.korean_name) AS surface_len
            FROM kair_platform_active_glossary_head h
            JOIN kair_platform_glossary_version_terms m
              ON m.glossary_id = h.glossary_id
             AND m.version = h.version
            JOIN kair_platform_terms t
              ON t.term_id = m.term_id
             AND t.revision = m.term_revision
             AND t.is_current
             AND t.status = 'APPROVED'
            JOIN kair_platform_standard_term_words stw
              ON stw.term_id = t.term_id
             AND stw.term_revision = t.revision
            JOIN kair_platform_standard_words w
              ON w.word_id = stw.word_id
             AND w.revision = stw.word_revision
             AND w.is_current
             AND w.status = 'APPROVED'
            WHERE char_length(w.korean_name) >= 2
              AND lower(regexp_replace(w.korean_name, '\\s+', '', 'g')) = ANY($1::text[])
            UNION ALL
            SELECT w.korean_name AS standard_term,
                   w.definition,
                   surface.surface,
                   w.korean_name AS word_korean,
                   char_length(surface.surface) AS surface_len
            FROM kair_platform_active_glossary_head h
            JOIN kair_platform_standard_words w
              ON w.glossary_id = h.glossary_id
             AND w.glossary_version = h.version
             AND w.is_current
             AND w.status = 'APPROVED'
            CROSS JOIN LATERAL (
                SELECT w.abbreviation AS surface
                WHERE char_length(COALESCE(w.abbreviation, '')) >= 2
                  AND lower(regexp_replace(w.abbreviation, '\\s+', '', 'g'))
                      = ANY($1::text[])
                UNION ALL
                SELECT w.english_name
                WHERE char_length(COALESCE(w.english_name, '')) >= 2
                  AND lower(regexp_replace(w.english_name, '\\s+', '', 'g'))
                      = ANY($1::text[])
                UNION ALL
                SELECT alias
                FROM jsonb_array_elements_text(w.aliases) AS alias
                WHERE char_length(alias) >= 2
                  AND lower(regexp_replace(alias, '\\s+', '', 'g'))
                      = ANY($1::text[])
            ) surface
        ) routes
        ORDER BY surface_len DESC, standard_term
        LIMIT 80
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, compact_needles)
        seen: set[tuple[str, str]] = set()
        routes: list[dict[str, Any]] = []
        for row in rows:
            surface = str(row.get("surface") or "").strip()
            standard = str(row.get("standard_term") or "").strip()
            word_korean = str(row.get("word_korean") or "").strip()
            if not surface or not standard:
                continue
            if not any(
                self._label_matches_needle(surface, needle)
                for needle in compact_needles
            ):
                continue
            key = (surface.casefold(), standard)
            if key in seen:
                continue
            seen.add(key)
            routes.append(
                {
                    "mention": surface,
                    "standard_term": standard,
                    "word_korean": word_korean or None,
                    "definition": str(row.get("definition") or "").strip() or None,
                }
            )
            if len(routes) >= 20:
                break
        return routes

    async def list_serving_tables(
        self,
        source_instance_id: str | None = None,
    ) -> list[dict[str, Any]]:
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
        JOIN t2s_snapshot_activations a
          ON a.source_instance_id=d.profile_id
         AND a.sink_name='t2s_serving'
         AND a.snapshot_id=t.metadata->>'snapshot_id'
        WHERE t.text_to_sql_is_valid = true
          AND t.review_status='approved'
          AND ($1::text IS NULL OR d.profile_id=$1)
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, source_instance_id)
        return [dict(row) for row in rows]

    async def fetch_approved_columns(
        self,
        table_ids: list[int],
    ) -> dict[int, list[dict[str, Any]]]:
        if not table_ids:
            return {}
        query = """
        SELECT c.id, c.table_id, c.name, c.fqn, c.dtype, c.nullable,
               c.description, c.analyzed_description, c.is_primary_key,
               c.metadata,
               t.schema_name, t.original_name AS table_name
        FROM t2s_columns c
        JOIN t2s_tables t ON t.id=c.table_id
        WHERE c.table_id = ANY($1::bigint[])
          AND c.review_status='approved'
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(query, table_ids)
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(int(row["table_id"]), []).append(dict(row))
        return grouped

    async def find_synonym_groups(
        self,
        needles: list[str] | None = None,
        *,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Approved synonym groups on the active glossary head.

        kind=type_suffix returns peel rows. kind=term (default) returns matching
        term groups for needles. Missing tables are not an error.
        """

        if kind == "type_suffix":
            return await self._find_type_suffix_synonym_groups()
        return await self._find_term_synonym_groups(needles)

    async def _find_type_suffix_synonym_groups(self) -> list[dict[str, Any]]:
        query = """
        SELECT g.group_name, m.synonym AS suffix, g.group_name AS kind,
               g.dictionary_markers
        FROM kair_platform_active_glossary_head h
        JOIN kair_platform_synonym_groups g
          ON g.glossary_id = h.glossary_id
         AND g.version = h.version
         AND g.kind = 'type_suffix'
         AND g.status = 'APPROVED'
        JOIN kair_platform_synonym_members m
          ON m.group_id = g.group_id
        """
        try:
            async with self._pool.acquire() as connection:
                rows = await connection.fetch(query)
        except Exception:
            return []
        return [dict(row) for row in rows]

    async def _find_term_synonym_groups(
        self,
        needles: list[str] | None,
    ) -> list[dict[str, Any]]:
        compact_needles = self._compact_needles(needles)
        if not compact_needles:
            return []
        count_query = """
        SELECT COUNT(*)
        FROM kair_platform_active_glossary_head h
        JOIN kair_platform_synonym_groups g
          ON g.glossary_id = h.glossary_id
         AND g.version = h.version
         AND g.kind = 'term'
         AND g.status = 'APPROVED'
        """
        try:
            async with self._pool.acquire() as connection:
                count = int(await connection.fetchval(count_query) or 0)
        except Exception:
            return await self._term_synonyms_from_word_aliases(compact_needles)
        if count <= 0:
            return await self._term_synonyms_from_word_aliases(compact_needles)
        query = """
        SELECT g.group_id, g.preferred_form, g.role, m.synonym
        FROM kair_platform_active_glossary_head h
        JOIN kair_platform_synonym_groups g
          ON g.glossary_id = h.glossary_id
         AND g.version = h.version
         AND g.kind = 'term'
         AND g.status = 'APPROVED'
        JOIN kair_platform_synonym_members m
          ON m.group_id = g.group_id
        WHERE EXISTS (
            SELECT 1
            FROM kair_platform_synonym_members hit
            WHERE hit.group_id = g.group_id
              AND lower(regexp_replace(hit.synonym, '\\s+', '', 'g'))
                  = ANY($1::text[])
        )
        """
        try:
            async with self._pool.acquire() as connection:
                rows = await connection.fetch(query, compact_needles)
        except Exception:
            return []
        return self._group_term_synonym_rows(rows)

    async def _term_synonyms_from_word_aliases(
        self,
        compact_needles: list[str],
    ) -> list[dict[str, Any]]:
        query = """
        SELECT w.word_id AS group_id,
               w.korean_name AS preferred_form,
               '용어'::text AS role,
               surface.surface AS synonym
        FROM kair_platform_active_glossary_head h
        JOIN kair_platform_standard_words w
          ON w.glossary_id = h.glossary_id
         AND w.glossary_version = h.version
         AND w.is_current
         AND w.status = 'APPROVED'
        CROSS JOIN LATERAL (
            SELECT w.korean_name AS surface
            UNION ALL
            SELECT w.abbreviation
            WHERE char_length(COALESCE(w.abbreviation, '')) >= 2
            UNION ALL
            SELECT w.english_name
            WHERE char_length(COALESCE(w.english_name, '')) >= 2
            UNION ALL
            SELECT alias
            FROM jsonb_array_elements_text(w.aliases) AS alias
            WHERE char_length(alias) >= 2
        ) surface
        WHERE char_length(COALESCE(surface.surface, '')) >= 2
          AND (
            lower(regexp_replace(surface.surface, '\\s+', '', 'g'))
                = ANY($1::text[])
            OR lower(regexp_replace(w.korean_name, '\\s+', '', 'g'))
                = ANY($1::text[])
          )
        """
        try:
            async with self._pool.acquire() as connection:
                rows = await connection.fetch(query, compact_needles)
        except Exception:
            return []
        return self._group_term_synonym_rows(rows)

    @staticmethod
    def _group_term_synonym_rows(rows: list[Any]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            group_id = str(row.get("group_id") or "")
            preferred = str(row.get("preferred_form") or "").strip()
            synonym = str(row.get("synonym") or "").strip()
            if not group_id or not preferred:
                continue
            item = grouped.setdefault(
                group_id,
                {
                    "kind": "term",
                    "role": str(row.get("role") or "용어"),
                    "preferred_form": preferred,
                    "members": [],
                },
            )
            members = item["members"]
            if synonym and synonym not in members:
                members.append(synonym)
            if preferred not in members:
                members.insert(0, preferred)
        return list(grouped.values())

    async def find_catalog_by_mentions(
        self,
        mentions: list[str],
        source_instance_id: str | None = None,
    ) -> list[dict[str, Any]]:
        tokens = [
            self._compact_natural_text(item)
            for item in mentions
            if self._compact_natural_text(item)
        ]
        if not tokens:
            return []
        tokens = self.expand_plant_mention_tokens(tokens)
        tables = await self.list_serving_tables(source_instance_id)
        columns = await self.fetch_approved_columns(
            [int(table["id"]) for table in tables]
        )
        hits: list[dict[str, Any]] = []
        for table in tables:
            hit = self._catalog_mention_hit(
                table,
                columns.get(int(table["id"]), []),
                tokens,
            )
            if hit is not None:
                hits.append(hit)
        return hits

    def _catalog_mention_hit(
        self,
        table: dict[str, Any],
        columns: list[dict[str, Any]],
        tokens: list[str],
    ) -> dict[str, Any] | None:
        logical = str(table.get("logical_name") or table.get("name") or "")
        for token in tokens:
            if len(token) >= 2 and self._label_matches_needle(logical, token):
                return self._with_catalog_match(
                    table, token, "exact", "logical_name", 1.0
                )
        surfaces: list[tuple[str, str]] = [
            ("logical_name", str(table.get("logical_name") or "")),
            ("description", str(table.get("description") or "")),
            ("analyzed_description", str(table.get("analyzed_description") or "")),
        ]
        for column in columns:
            metadata = column.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            surfaces.extend(
                [
                    ("column.description", str(column.get("description") or "")),
                    (
                        "column.analyzed_description",
                        str(column.get("analyzed_description") or ""),
                    ),
                    ("column_name_kr", str(metadata.get("column_name_kr") or "")),
                    ("column.logical_name", str(metadata.get("logical_name") or "")),
                ]
            )
        for field, surface in surfaces:
            for token in tokens:
                if len(token) < 2:
                    continue
                if self._label_matches_needle(surface, token):
                    score = 1.0 if field == "logical_name" else 0.85
                    return self._with_catalog_match(
                        table, token, "exact", field, score
                    )
                if self._label_starts_with(surface, token):
                    return self._with_catalog_match(
                        table, token, "prefix", field, 0.6
                    )
        return None

    @staticmethod
    def _with_catalog_match(
        table: dict[str, Any],
        mention: str,
        match_type: str,
        matched_field: str,
        score: float,
    ) -> dict[str, Any]:
        hit = dict(table)
        hit["matched_mention"] = mention
        hit["match_type"] = match_type
        hit["matched_field"] = matched_field
        hit["score"] = score
        return hit

    @staticmethod
    def _label_starts_with(label: str, prefix: str) -> bool:
        haystack = SearchMixin._compact_natural_text(label)
        needle = SearchMixin._compact_natural_text(prefix)
        return bool(needle) and len(needle) >= 2 and haystack.startswith(needle)

    @staticmethod
    def prefixes_for_unmatched(
        tokens: list[str],
        matched_labels: list[str],
    ) -> list[str]:
        labels = [SearchMixin._compact_natural_text(item) for item in matched_labels]
        prefixes: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            compact = SearchMixin._compact_natural_text(token)
            if len(compact) < 3:
                continue
            if any(SearchMixin._label_starts_with(label, compact) for label in labels):
                continue
            for length in range(len(compact) - 1, 1, -1):
                piece = compact[:length]
                if piece in seen:
                    continue
                seen.add(piece)
                prefixes.append(piece)
        return prefixes

    @staticmethod
    def _compact_natural_text(value: str) -> str:
        """Drop whitespace so Store labels like '탁 도' match query '탁도'."""
        return re.sub(r"\s+", "", value or "").casefold()

    @staticmethod
    def _mapping_looks_like_measure_code(row: dict[str, Any]) -> bool:
        """별량/측정항목 dictionaries. Location codes still allow prefix reverse."""

        blob = SearchMixin._compact_natural_text(
            " ".join(
                [
                    str(row.get("logical_name") or ""),
                    str(row.get("column_fqn") or ""),
                    str(row.get("column_name") or ""),
                ]
            )
        )
        if any(word in blob for word in ("사업장", "정수장", "sujcode")):
            return False
        return any(word in blob for word in ("별량", "측정항목"))

    @staticmethod
    def _natural_label_head(natural: str) -> str:
        """Korean head of a store label. Parenthetical aliases are not the head."""

        head = re.split(r"[\(（]", str(natural or ""), maxsplit=1)[0]
        return SearchMixin._compact_natural_text(head)

    @staticmethod
    def _measure_prefix_unlicensed(
        question: str,
        token: str,
        natural: str,
    ) -> bool:
        """True when a shorter mention is only a prefix of a longer measure label.

        The leftover head must appear in the question, otherwise this is not a
        licensed bind (설비 ⊂ 설비온도). Equal heads (강우량 ⊂ 강우량(Rainfall))
        are licensed.
        """

        head = SearchMixin._natural_label_head(natural)
        token_c = SearchMixin._compact_natural_text(token)
        if not token_c or not head.startswith(token_c) or head == token_c:
            return False
        remainder = head[len(token_c) :]
        if not remainder:
            return False
        return remainder not in SearchMixin._compact_natural_text(question)

    @staticmethod
    def _is_hangul_char(ch: str) -> bool:
        return bool(ch) and "\uac00" <= ch <= "\ud7a3"

    @staticmethod
    def _question_mention_tokens(
        question: str,
        extra: list[str] | None = None,
    ) -> list[str]:
        """Surface tokens for reverse mapping lookup (token ⊂ Store label).

        Does not invent abbreviations. Uses Hangul/Latin runs already present
        in the question or analyzer surfaces, minus generic stopwords.
        """
        tokens: list[str] = []
        seen: set[str] = set()
        for text in (question, *(extra or [])):
            for raw in _MENTION_TOKEN.findall(text or ""):
                token = raw
                changed = True
                while changed:
                    changed = False
                    for suffix in _PARTICLE_SUFFIXES:
                        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
                            token = token[: -len(suffix)]
                            changed = True
                            break
                compact = SearchMixin._compact_natural_text(token)
                if len(compact) < 2 or compact.isdigit():
                    continue
                if compact in _REVERSE_LOOKUP_STOPWORDS:
                    continue
                if compact in seen:
                    continue
                seen.add(compact)
                tokens.append(compact)
        return tokens

    @staticmethod
    def _token_is_label_mention(token: str, label: str) -> bool:
        """True when the store label starts with the mention token."""

        return SearchMixin._label_starts_with(label, token)

    @staticmethod
    def _mapping_table_key(row: dict[str, Any]) -> tuple[Any, ...]:
        table_id = row.get("table_id")
        if table_id is not None and str(table_id).strip() != "":
            try:
                return ("id", int(table_id))
            except (TypeError, ValueError):
                pass
        table = str(row.get("table_name") or row.get("original_name") or "").strip()
        if table:
            return (
                "name",
                str(row.get("db") or "").casefold(),
                str(row.get("schema_name") or "").casefold(),
                table.casefold(),
            )
        fqn = str(row.get("column_fqn") or "").strip()
        parts = [part for part in fqn.split(".") if part]
        if len(parts) >= 2:
            return ("fqn", ".".join(parts[:-1]).casefold())
        return ("row", id(row))

    @staticmethod
    def _select_mentioned_mappings(
        needles: list[str] | str,
        rows: list[dict[str, Any]],
        mention_tokens: list[str],
        trusted_extras: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Keep equality/head hits per needle. Extra tokens are synonyms only."""

        needle_list = (
            [needles]
            if isinstance(needles, str)
            else [str(item) for item in needles]
        )
        needle_list = SearchMixin._compact_needles(needle_list)
        extras = {
            SearchMixin._compact_natural_text(item)
            for item in (trusted_extras or set())
            if SearchMixin._compact_natural_text(item)
        }
        extra_tokens = SearchMixin._compact_needles(mention_tokens)
        exact: list[dict[str, Any]] = []
        reverse: list[dict[str, Any]] = []
        for row in rows:
            natural = str(row.get("natural_value") or "")
            code = SearchMixin._compact_natural_text(str(row.get("code_value") or ""))
            matched_needle = next(
                (
                    needle
                    for needle in needle_list
                    if SearchMixin._label_matches_needle(natural, needle)
                ),
                "",
            )
            if matched_needle:
                item = dict(row)
                item["matched_mention"] = matched_needle
                item["match_type"] = "exact"
                item["matched_field"] = "natural_value"
                item["score"] = 1.0
                exact.append(item)
                continue
            matched = ""
            match_type = ""
            matched_field = ""
            for token in extra_tokens:
                if token not in extras:
                    continue
                if SearchMixin._token_is_label_mention(token, natural):
                    if (
                        SearchMixin._mapping_looks_like_measure_code(row)
                        and SearchMixin._measure_prefix_unlicensed(
                            token, token, natural
                        )
                    ):
                        continue
                    matched = token
                    match_type = "alias_prefix"
                    matched_field = "natural_value"
                    break
                if code and len(code) >= 2 and code == token:
                    matched = token
                    match_type = "code"
                    matched_field = "code_value"
                    break
            if not matched:
                continue
            item = dict(row)
            item["matched_mention"] = matched
            item["match_type"] = match_type
            item["matched_field"] = matched_field
            item["score"] = 0.8 if match_type == "alias_prefix" else 0.9
            reverse.append(item)
        exact_by_label: dict[str, set[str]] = {}
        for row in exact:
            label = SearchMixin._compact_natural_text(
                str(row.get("natural_value") or "")
            )
            code = str(row.get("code_value") or "").strip()
            if label:
                exact_by_label.setdefault(label, set()).add(code)
        kept_exact: list[dict[str, Any]] = []
        for row in exact:
            label = SearchMixin._compact_natural_text(
                str(row.get("natural_value") or "")
            )
            code = str(row.get("code_value") or "").strip()
            if label and any(
                other.startswith(label)
                and other != label
                and code
                and code in exact_by_label.get(other, set())
                for other in exact_by_label
            ):
                continue
            kept_exact.append(row)
        kept_by_label: dict[str, set[str]] = {}
        for row in kept_exact:
            label = SearchMixin._compact_natural_text(
                str(row.get("natural_value") or "")
            )
            code = str(row.get("code_value") or "").strip()
            if label:
                kept_by_label.setdefault(label, set()).add(code)
        exact_tables = {
            SearchMixin._mapping_table_key(row) for row in kept_exact
        }
        kept_reverse: list[dict[str, Any]] = []
        for row in reverse:
            if exact_tables and SearchMixin._mapping_table_key(row) not in exact_tables:
                continue
            token = SearchMixin._compact_natural_text(
                str(row.get("matched_mention") or "")
            )
            code = str(row.get("code_value") or "").strip()
            if token and any(
                label.startswith(token)
                and label != token
                and code
                and code in kept_by_label.get(label, set())
                for label in kept_by_label
            ):
                continue
            kept_reverse.append(row)
        return [*kept_exact, *kept_reverse]

    @staticmethod
    def _surface_is_standalone_mention(question: str, surface: str) -> bool:
        """Standalone mention. Latin/alnum surfaces need a word boundary."""

        text = str(surface or "").strip()
        if not text:
            return False
        compact = SearchMixin._compact_natural_text(text)
        if compact and not SearchMixin._is_hangul_char(compact[0]):
            pattern = r"(?<![A-Za-z0-9])" + re.escape(text) + r"(?![A-Za-z0-9])"
            if re.search(pattern, question, flags=re.IGNORECASE):
                return True
            if compact != text.casefold():
                compact_pattern = (
                    r"(?<![A-Za-z0-9])" + re.escape(compact) + r"(?![A-Za-z0-9])"
                )
                if re.search(compact_pattern, question, flags=re.IGNORECASE):
                    return True
            return False
        return SearchMixin._natural_value_is_standalone_mention(question, text)

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

        for match in re.finditer(pattern, question, flags=re.IGNORECASE):
            before = question[match.start() - 1] if match.start() > 0 else ""
            immediate_after = (
                question[match.end()] if match.end() < len(question) else ""
            )
            # Whitespace or EOS is a token boundary (do not look past spaces into
            # the next Hangul word — that rejected '화성정수장 평균 …').
            if not SearchMixin._is_hangul_char(before) and (
                not immediate_after
                or immediate_after.isspace()
                or not SearchMixin._is_hangul_char(immediate_after)
            ):
                return True
            if not SearchMixin._is_hangul_char(before) and SearchMixin._is_hangul_char(
                immediate_after
            ):
                rest = question[match.end() :]
                if any(rest.startswith(suffix) for suffix in _PARTICLE_SUFFIXES):
                    return True
        return False
