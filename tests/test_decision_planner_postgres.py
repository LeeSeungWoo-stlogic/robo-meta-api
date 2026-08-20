from __future__ import annotations

import unittest
from pathlib import Path

from app import runtime_config
from app.runtime_config import (
    DecisionRuntime,
    EmbeddingRuntime,
    ExecutionRuntime,
    MetadataRuntime,
    RoboRuntime,
)
from app.schemas import (
    FilterRequirement,
    JoinRequirement,
    MeasurementRequirement,
    QueryAnalysis,
    QueryPlan,
    QuerySearchKeywords,
    SchemaRoleRequirement,
)
from app.services.decision_planner import (
    build_composite_edges,
    include_mapped_tables,
    merge_axis_candidates,
    select_minimal_tables,
)
from app.services.decision_postgres import _candidate, _resolved_entities, decide
from app.services.decision_postgres.helpers import glossary_surfaces
from app.services.embedding_provider import set_embedding_provider
from app.services.query_analysis import _sanitize, set_query_analyzer


def table(table_id: int, name: str, score: float) -> dict:
    return {
        "id": table_id,
        "db": "rwis",
        "source_name": "RWIS",
        "schema_name": "RWIS",
        "name": name,
        "original_name": name,
        "source_instance_id": "rwis-pg",
        "subject_area": "master",
        "score": score,
    }


def edge_row(
    from_id: int,
    from_table: str,
    from_column: str,
    to_id: int,
    to_table: str,
    to_column: str,
    confidence: float,
) -> dict:
    return {
        "from_table_id": from_id,
        "from_schema": "RWIS",
        "from_table": from_table,
        "from_column": from_column,
        "to_table_id": to_id,
        "to_schema": "RWIS",
        "to_table": to_table,
        "to_column": to_column,
        "metadata": {
            "origins": ["script_mined"],
            "confidence": confidence,
        },
    }


class DecisionPlannerTests(unittest.TestCase):
    def test_resolved_entity_uses_mapping_label_column(self) -> None:
        entities = _resolved_entities(
            [
                {
                    "natural_value": "청주",
                    "code_value": "CJJ",
                    "db": "rwis",
                    "schema_name": "RWIS",
                    "table_name": "FACILITY_TB",
                    "column_name": "FACILITY_CODE",
                    "metadata": '{"label_column": "FACILITY_NAME"}',
                }
            ]
        )
        self.assertEqual(entities[0].name_column, "FACILITY_NAME")
        self.assertEqual(entities[0].code_column, "FACILITY_CODE")
        self.assertEqual(entities[0].values[0].code, "CJJ")
        self.assertEqual(entities[0].values[0].label, "청주")
        self.assertEqual(entities[0].source, "value_examples")
        self.assertIsNone(entities[0].matched_mention)

    def test_resolved_entity_uses_glossary_source_for_term_alias(self) -> None:
        entities = _resolved_entities(
            [
                {
                    "natural_value": "탁 도",
                    "code_value": "TB",
                    "db": "rwis",
                    "schema_name": "RWIS",
                    "table_name": "RDIBYUN_TB",
                    "column_name": "BR_CODE",
                    "matched_mention": "NTU",
                    "match_type": "alias_prefix",
                }
            ],
            glossary_surfaces={"ntu", "탁도"},
        )
        self.assertEqual(entities[0].source, "glossary")
        self.assertEqual(entities[0].values[0].code, "TB")
        self.assertEqual(entities[0].matched_mention, "NTU")
        self.assertEqual(entities[0].mention, "탁 도")

    def test_resolved_entity_uses_glossary_source_for_standard_term(self) -> None:
        entities = _resolved_entities(
            [
                {
                    "natural_value": "탁 도",
                    "code_value": "TB",
                    "db": "rwis",
                    "schema_name": "RWIS",
                    "table_name": "RDIBYUN_TB",
                    "column_name": "BR_CODE",
                    "matched_mention": "탁도",
                    "match_type": "exact",
                }
            ],
            glossary_surfaces={"탁도", "ntu"},
        )
        self.assertEqual(entities[0].source, "glossary")
        self.assertEqual(entities[0].matched_mention, "탁도")

    def test_resolved_entity_uses_glossary_source_for_suffix_peel(self) -> None:
        entities = _resolved_entities(
            [
                {
                    "natural_value": "금강유역본부",
                    "code_value": "701",
                    "db": "rwis",
                    "schema_name": "RWIS",
                    "table_name": "RDIBONBU_TB",
                    "column_name": "BNB_CODE",
                    "matched_mention": "금강유역",
                    "match_type": "exact",
                }
            ]
        )
        self.assertEqual(entities[0].source, "glossary")
        self.assertEqual(entities[0].matched_mention, "금강유역")

    def test_resolved_entity_copies_query_surface_as_matched_mention(self) -> None:
        entities = _resolved_entities(
            [
                {
                    "natural_value": "탁 도",
                    "code_value": "TB",
                    "db": "rwis",
                    "schema_name": "RWIS",
                    "table_name": "rdibyun_tb",
                    "column_name": "BR_CODE",
                    "matched_mention": "탁도값",
                    "match_type": "exact",
                    "metadata": '{"label_column": "BR_NAME"}',
                }
            ],
            glossary_surfaces={"탁도값", "탁도"},
        )
        self.assertEqual(entities[0].mention, "탁 도")
        self.assertEqual(entities[0].matched_mention, "탁도값")
        self.assertEqual(entities[0].table, "rdibyun_tb")
        self.assertEqual(entities[0].name_column, "BR_NAME")
        self.assertEqual(entities[0].code_column, "BR_CODE")
        self.assertEqual(entities[0].values[0].code, "TB")
        self.assertEqual(entities[0].values[0].label, "탁 도")
        self.assertEqual(entities[0].source, "glossary")

    def test_glossary_surfaces_uses_term_groups_not_type_suffix(self) -> None:
        surfaces = glossary_surfaces(
            [
                {"kind": "term", "preferred_form": "탁 도", "members": ["NTU", "탁도"]},
                {"kind": "type_suffix", "preferred_form": "유역", "members": ["권역"]},
            ],
            [{"mention": "탁도", "standard_term": "탁도", "word_korean": "탁도"}],
        )
        self.assertIn("탁도", surfaces)
        self.assertIn("ntu", surfaces)
        self.assertNotIn("유역", surfaces)
        self.assertNotIn("권역", surfaces)

    def test_candidate_maps_column_metadata_to_value_examples(self) -> None:
        candidate = _candidate(
            table(1, "MEASURE_TB", 0.9),
            [
                {
                    "name": "VALUE",
                    "score": 0.8,
                    "is_primary_key": True,
                    "is_foreign_key": True,
                    "metadata": {
                        "sample_values": [
                            {"value": 1.2},
                            "2.3",
                            {"value": "3.4"},
                            "4.5",
                            "5.6",
                            "ignored",
                        ],
                        "format_pattern": "0.0",
                        "unit": "mg/L",
                        "pk_ordinal": "2",
                    },
                }
            ],
            source="vector",
        )
        matched = candidate.matched_columns[0]
        self.assertEqual(matched.constraints, ["PK", "FK"])
        self.assertEqual(
            matched.value_examples,
            ["1.2", "2.3", "3.4", "4.5", "5.6"],
        )
        self.assertEqual(matched.format_pattern, "0.0")
        self.assertEqual(matched.unit, "mg/L")
        self.assertEqual(matched.pk_ordinal, 2)
        self.assertIsNone(matched.facility_code)
        self.assertIsNone(matched.system_code)
        self.assertEqual(matched.has_code, "N")

    def test_candidate_marks_only_value_mapping_code_columns(self) -> None:
        candidate = _candidate(
            table(1, "RDIBYUN_TB", 0.9),
            [
                {"name": "BR_CODE", "score": 0.9, "metadata": {}},
                {"name": "BR_NAME", "score": 0.8, "metadata": {}},
                {"name": "BR_ORDER", "score": 0.1, "metadata": {}},
            ],
            source="name_rule",
            code_columns={"BR_CODE"},
        )
        by_name = {item.column_name: item.has_code for item in candidate.matched_columns}
        self.assertEqual(by_name["BR_CODE"], "Y")
        self.assertEqual(by_name["BR_NAME"], "N")
        self.assertEqual(by_name["BR_ORDER"], "N")

    def test_candidate_maps_logical_name_as_hint_not_identifier(self) -> None:
        row = table(1, "RDITAG_TB", 0.9)
        row["logical_name"] = "태그 마스터"
        candidate = _candidate(row, [], source="vector")
        self.assertEqual(candidate.table_name, "RDITAG_TB")
        self.assertEqual(candidate.logical_name, "태그 마스터")

        blank = table(2, "OTHER_TB", 0.5)
        blank["logical_name"] = "「미정」"
        self.assertIsNone(_candidate(blank, [], source="vector").logical_name)

    def test_candidate_maps_facility_and_system_codes(self) -> None:
        candidate = _candidate(
            table(1, "MEASURE_TB", 0.9),
            [
                {
                    "name": "VALUE",
                    "score": 0.8,
                    "is_primary_key": False,
                    "is_foreign_key": False,
                    "metadata": {
                        "facility_code": "FAC1",
                        "system_code": "SYS9",
                        "facility_scope": "ignored-when-facility_code-present",
                    },
                }
            ],
            source="vector",
        )
        matched = candidate.matched_columns[0]
        self.assertEqual(matched.facility_code, "FAC1")
        self.assertEqual(matched.system_code, "SYS9")

    def test_composite_edge_groups_multi_column_join(self) -> None:
        edges = build_composite_edges(
            [
                edge_row(1, "RDISAUP_TB", "BNB_CODE", 2, "RDISAMU_TB", "BNB_CODE", 0.9),
                edge_row(1, "RDISAUP_TB", "SMS_CODE", 2, "RDISAMU_TB", "SMS_CODE", 0.8),
            ]
        )
        self.assertEqual(len(edges), 1)
        self.assertEqual(len(edges[0].conditions), 2)
        self.assertEqual(edges[0].confidence, 0.8)

    def test_minimal_selection_adds_two_hop_bridge(self) -> None:
        left = table(1, "FACT_TB", 0.95)
        bridge = table(2, "TAG_TB", 0.7)
        right = table(3, "FACILITY_TB", 0.9)
        edges = build_composite_edges(
            [
                edge_row(1, "FACT_TB", "TAGSN", 2, "TAG_TB", "TAGSN", 1.0),
                edge_row(2, "TAG_TB", "SUJ_CODE", 3, "FACILITY_TB", "SUJ_CODE", 0.9),
            ]
        )
        selection = select_minimal_tables(
            required_roles=["측정 fact", "시설 master"],
            optional_roles=[],
            role_candidates={
                "측정 fact": [left],
                "시설 master": [right],
            },
            edges=edges,
            max_hops=3,
            table_limit=10,
        )
        self.assertEqual(selection.selected_table_ids, {1, 2, 3})
        self.assertEqual(selection.bridge_table_ids, {2})
        self.assertEqual(len(selection.paths), 1)
        self.assertEqual(len(selection.paths[0]), 2)
        self.assertEqual(selection.unresolved, [])

    def test_disconnected_required_role_is_explicitly_unresolved(self) -> None:
        selection = select_minimal_tables(
            required_roles=["측정 fact", "시설 master"],
            optional_roles=[],
            role_candidates={
                "측정 fact": [table(1, "FACT_TB", 0.95)],
                "시설 master": [table(3, "FACILITY_TB", 0.9)],
            },
            edges=[],
            max_hops=3,
            table_limit=10,
        )
        self.assertIn("승인 JOIN 경로 없음: 시설 master", selection.unresolved)

    def test_include_mapped_tables_keeps_hub_and_join_path(self) -> None:
        left = table(1, "FACT_TB", 0.95)
        edges = build_composite_edges(
            [
                edge_row(1, "FACT_TB", "SUJ_CODE", 3, "FACILITY_TB", "SUJ_CODE", 0.9),
            ]
        )
        selection = select_minimal_tables(
            required_roles=["측정 fact"],
            optional_roles=[],
            role_candidates={"측정 fact": [left]},
            edges=edges,
            max_hops=3,
            table_limit=10,
        )
        self.assertEqual(selection.selected_table_ids, {1})
        include_mapped_tables(
            selection,
            mapped_table_ids={3},
            edges=edges,
            max_hops=3,
        )
        self.assertIn(3, selection.selected_table_ids)
        self.assertEqual(len(selection.paths), 1)

    def test_include_mapped_tables_keeps_hub_without_join(self) -> None:
        selection = select_minimal_tables(
            required_roles=["측정 fact"],
            optional_roles=[],
            role_candidates={"측정 fact": [table(1, "FACT_TB", 0.95)]},
            edges=[],
            max_hops=3,
            table_limit=10,
        )
        include_mapped_tables(
            selection,
            mapped_table_ids={9},
            edges=[],
            max_hops=3,
        )
        self.assertEqual(selection.selected_table_ids, {1, 9})
        self.assertIn(9, selection.bridge_table_ids)

    def test_multi_axis_merge_preserves_role_signal(self) -> None:
        first = table(1, "FACT_TB", 0.8)
        second = table(2, "MASTER_TB", 0.7)
        merged = merge_axis_candidates(
            {
                "question": [first, second],
                "hyde": [{**first, "score": 0.9}],
                "role:master": [{**second, "score": 0.95}],
            },
            question_weight=0.4,
            hyde_weight=0.6,
            role_weight=0.35,
            limit=10,
        )
        self.assertEqual({row["id"] for row in merged}, {1, 2})
        by_id = {row["id"]: row for row in merged}
        self.assertEqual(by_id[2]["role_scores"], {"master": 0.95})

    def test_additive_response_contract_accepts_degraded_plan(self) -> None:
        analysis = QueryAnalysis(
            status="degraded",
            reason="provider unavailable",
            fallback="question_vector",
        )
        plan = QueryPlan(
            completeness="degraded",
            unresolved_requirements=["역할별 최소 집합 미확정"],
        )
        self.assertEqual(analysis.status, "degraded")
        self.assertEqual(plan.completeness, "degraded")

    def test_column_only_message_role_is_folded_into_parent(self) -> None:
        analysis = _sanitize(
            QueryAnalysis(
                status="complete",
                intent="연결 오류 조회",
                schema_roles=[
                    SchemaRoleRequirement(
                        role="사무소 연결 상태",
                        search_terms=["연결 상태"],
                    ),
                    SchemaRoleRequirement(
                        role="오류 메시지",
                        search_terms=["오류 설명"],
                    ),
                ],
                join_requirements=[
                    JoinRequirement(
                        from_role="사무소 연결 상태",
                        to_role="오류 메시지",
                    )
                ],
            )
        )
        self.assertEqual(
            [role.role for role in analysis.schema_roles],
            ["사무소 연결 상태"],
        )
        self.assertEqual(analysis.join_requirements, [])


class FakeAnalyzer:
    async def analyze(self, question: str, timeout_s=None, store_hits=None) -> QueryAnalysis:
        del question, store_hits
        return QueryAnalysis(
            status="complete",
            intent="사업장별 연결 오류 조회",
            measurement=MeasurementRequirement(metric="연결 오류"),
            schema_roles=[
                SchemaRoleRequirement(
                    role="사업장 마스터",
                    necessity="required",
                    cardinality="one",
                ),
                SchemaRoleRequirement(
                    role="사무소 연결 상태",
                    necessity="required",
                    cardinality="many",
                ),
            ],
            join_requirements=[
                JoinRequirement(
                    from_role="사업장 마스터",
                    to_role="사무소 연결 상태",
                    key_meanings=["본부 코드", "사무소 코드"],
                )
            ],
            filter_requirements=[
                FilterRequirement(
                    meaning="오류가 있는 항목",
                    operator_hint="IS_NOT_NULL",
                )
            ],
            search_keywords=QuerySearchKeywords(
                tables=["사업장", "사무소"],
                columns=["오류 메시지"],
            ),
        )


class FakeEmbeddingProvider:
    model = "fake"
    dimensions = 3

    async def embed(self, text: str) -> list[float]:
        return [float(len(text)), 0.0, 0.0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [
            [float(index + 1), 0.0, 0.0]
            for index, _ in enumerate(texts)
        ]


class FakeDegradedAnalyzer:
    async def analyze(self, question: str, timeout_s=None, store_hits=None) -> QueryAnalysis:
        del question, store_hits
        return QueryAnalysis(
            status="degraded",
            reason="provider unavailable",
            fallback="question_vector",
        )


class FakeRepository:
    def __init__(self) -> None:
        self.tables = {
            1: table(1, "RDISAUP_TB", 0.9),
            2: table(2, "RDISAMU_TB", 0.95),
        }

    async def search_tables(
        self,
        embedding,
        *,
        limit,
        source_instance_id=None,
    ):
        del limit, source_instance_id
        if embedding[0] == 3.0:
            return [{**self.tables[1], "score": 0.96}]
        if embedding[0] == 4.0:
            return [{**self.tables[2], "score": 0.97}]
        return [
            {**self.tables[2], "score": 0.9},
            {**self.tables[1], "score": 0.85},
        ]

    async def execution_source_scope(self, source_instance_id):
        if source_instance_id != "rwis-pg":
            return None
        return {
            "source_instance_id": source_instance_id,
            "engine": "postgresql",
            "source_schema": "RWIS",
            "mindsdb_integration": "rwis",
            "mindsdb_catalog": "rwis",
            "source_name": "RWIS",
            "allowed_objects": ["RDISAUP_TB", "RDISAMU_TB"],
            "allowed_schemas": ["RWIS"],
            "allowed_object_refs": [
                {"schema_name": "RWIS", "original_name": "RDISAUP_TB"},
                {"schema_name": "RWIS", "original_name": "RDISAMU_TB"},
            ],
        }

    async def find_value_mappings(
        self, needles=None, source_instance_id=None, extra_mentions=None, trusted_mentions=None
    ):
        del needles, source_instance_id, extra_mentions, trusted_mentions
        return []

    async def find_value_mapping_code_columns(self, table_ids):
        del table_ids
        return {}

    async def find_glossary_routes(self, needles=None):
        del needles
        return []

    async def fetch_tables_by_ids(self, table_ids):
        return [dict(self.tables[item]) for item in table_ids if item in self.tables]

    async def fetch_join_edges(self, *, source_instance_id):
        self.assert_source = source_instance_id
        return [
            edge_row(1, "RDISAUP_TB", "BNB_CODE", 2, "RDISAMU_TB", "BNB_CODE", 0.9),
            edge_row(1, "RDISAUP_TB", "SMS_CODE", 2, "RDISAMU_TB", "SMS_CODE", 0.9),
        ]

    async def search_columns(self, embedding, *, table_ids, per_table_limit):
        del embedding, per_table_limit
        return {
            table_id: [
                {
                    "id": table_id * 100,
                    "table_id": table_id,
                    "name": "LK_ERRMSG" if table_id == 2 else "SUJ_NAME",
                    "fqn": (
                        "RWIS.RDISAMU_TB.LK_ERRMSG"
                        if table_id == 2
                        else "RWIS.RDISAUP_TB.SUJ_NAME"
                    ),
                    "dtype": "varchar",
                    "is_primary_key": False,
                    "score": 0.9 if table_id == 2 else 0.5,
                }
            ]
            for table_id in table_ids
        }

    async def find_catalog_by_mentions(self, mentions, source_instance_id=None):
        del mentions, source_instance_id
        return [dict(self.tables[1]), dict(self.tables[2])]

    async def fk_neighbor_table_ids(self, table_ids):
        neighbors: set[int] = set()
        if 1 in table_ids:
            neighbors.add(2)
        if 2 in table_ids:
            neighbors.add(1)
        return neighbors

    async def fetch_approved_columns(self, table_ids):
        return await self.search_columns(None, table_ids=table_ids, per_table_limit=5)


class DecisionOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous_runtime = runtime_config._runtime
        runtime_config._runtime = RoboRuntime(
            settings_path=Path("test.yaml"),
            api_host="127.0.0.1",
            api_port=8100,
            metadata_backend="postgres",
            metadata=MetadataRuntime("host", 5432, "db", "public", "u", "p"),
            embedding=EmbeddingRuntime(
                "http://openai.test/v1",
                "fake",
                3,
                "none",
                None,
                1,
            ),
            decision=DecisionRuntime(
                10,
                5,
                0.2,
                1.0,
                0.7,
                role_top_k=3,
                score_gap_ratio=0.0,
                score_min_step=0.0,
                score_top_radius=0.0,
            ),
            execution=ExecutionRuntime(
                backend="mindsdb",
                sql_api_url="http://mindsdb.test",
                default_timeout_seconds=1,
                maximum_timeout_seconds=2,
                default_max_rows=10,
                maximum_rows=20,
                maximum_response_bytes=1000,
                audit_log_path="audit.jsonl",
            ),
        )
        set_query_analyzer(FakeAnalyzer())
        set_embedding_provider(FakeEmbeddingProvider())

    def tearDown(self) -> None:
        runtime_config._runtime = self.previous_runtime
        set_query_analyzer(None)
        set_embedding_provider(None)

    async def test_complete_multi_table_plan_limits_execution_context(self) -> None:
        response = await decide(
            FakeRepository(),
            query="사업장 이름별 사무소 연결 오류",
            include_matched_columns=True,
            column_top_m=5,
            auto_resolve_entities=True,
        )
        self.assertEqual(response.query_analysis.status, "complete")
        self.assertEqual(response.query_plan.completeness, "complete")
        self.assertEqual(len(response.query_plan.required_tables), 2)
        self.assertEqual(len(response.query_plan.join_paths), 1)
        self.assertEqual(
            len(response.query_plan.join_paths[0].conditions),
            2,
        )
        self.assertEqual(
            set(response.execution_context.allowed_objects),
            {"RDISAUP_TB", "RDISAMU_TB"},
        )

    async def test_degraded_analysis_returns_candidates_without_execution_context(
        self,
    ) -> None:
        set_query_analyzer(FakeDegradedAnalyzer())
        response = await decide(
            FakeRepository(),
            query="사업장 연결 상태",
            include_matched_columns=True,
            column_top_m=5,
            auto_resolve_entities=True,
        )
        self.assertEqual(response.query_analysis.status, "degraded")
        self.assertEqual(response.query_plan.completeness, "partial")
        self.assertTrue(response.candidates)

    def test_sanitize_turns_series_list_into_lookup(self) -> None:
        analysis = _sanitize(
            QueryAnalysis(
                status="complete",
                procedure="list",
                metric="탁도",
                goal="충주정수장의 2025년 8월 탁도 측정값 변화를 확보한다",
            ),
            "충주정수장 2025년 8월 탁도변화 알려줘",
        )
        self.assertEqual(analysis.procedure, "lookup")

    def test_sanitize_keeps_subject_list(self) -> None:
        analysis = _sanitize(
            QueryAnalysis(
                status="complete",
                procedure="list",
                metric="",
                goal="금강권역에 속하는 정수장 목록을 확보한다",
            ),
            "금강권역 정수장 목록",
        )
        self.assertEqual(analysis.procedure, "list")


if __name__ == "__main__":
    unittest.main()
