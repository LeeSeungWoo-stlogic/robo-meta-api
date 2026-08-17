"""POST /t2sql fixture gates. Do not boot app.main lifespan / live Store."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import runtime_config
from app.runtime_config import (
    DecisionRuntime,
    EmbeddingRuntime,
    ExecutionRuntime,
    MetadataRuntime,
    RoboRuntime,
    T2SqlRuntime,
    load_runtime,
)
from app.routers import t2sql as t2sql_router
from app.schemas import (
    META_VERSION,
    DecisionCandidate,
    DecisionResponse,
    ExecutionContext,
    JoinGroup,
    MatchedColumn,
    PlannedFilter,
    PlannedTable,
    QueryAnalysis,
    QueryPlan,
    ResolvedEntity,
    ResolvedValue,
    T2SqlRequest,
    TableKey,
)
from app.services.sql_guard import GuardError
from app.services.t2sql import (
    reset_t2sql_llm,
    run_t2sql,
    set_t2sql_decide,
    set_t2sql_execute,
    set_t2sql_llm,
)
from app.services.t2sql.probe import (
    assert_sql_in_allowlist,
    build_entity_probe_sql,
    entity_needs_live_probe,
    probe_allowlist,
)


SOURCE_ID = "source-rwis"
FACT_SQL = (
    "SELECT AVG(val) FROM `RWIS`.`RWIS`.`RDF01HH_TB` "
    "WHERE suj_code = '617'"
)
FACT_SQL_NO_WHERE = "SELECT AVG(val) FROM `RWIS`.`RWIS`.`RDF01HH_TB`"
FACT_SQL_PUBLIC = (
    "SELECT AVG(t1.`val`) FROM `RWIS`.`RDF01HH_TB` AS t1 "
    "WHERE t1.`suj_code` = '617'"
)
FACT_SQL_NO_WHERE_PUBLIC = (
    "SELECT AVG(t1.`val`) FROM `RWIS`.`RDF01HH_TB` AS t1"
)
MONTH_FACT_SQL = (
    "SELECT AVG(val) FROM `RWIS`.`RWIS`.`RDD01MM_TB` "
    "WHERE suj_code = '358'"
)
DAY_FACT_SQL = (
    "SELECT AVG(val) FROM `RWIS`.`RWIS`.`RDD01DD_TB` "
    "WHERE suj_code = '358'"
)


def _norm_sql(sql: str | None) -> str:
    return " ".join((sql or "").split())


class FakeRepository:
    def __init__(self, state):
        self.state = state

    async def execution_source_scope(self, source_instance_id):
        if self.state["source_instance_id"] == source_instance_id:
            return dict(self.state)
        return None

    async def find_profile_ids_by_source_name(self, name):
        if str(self.state.get("source_name") or "").lower() == str(name).lower():
            return [self.state["source_instance_id"]]
        return []


def _runtime(*, configured: bool = True) -> RoboRuntime:
    return RoboRuntime(
        settings_path=ROOT / "synthetic-settings.yaml",
        api_host="127.0.0.1",
        api_port=8100,
        metadata_backend="postgres",
        metadata=MetadataRuntime("host", 5432, "t2s", "public", "user", "pw"),
        embedding=EmbeddingRuntime(
            "http://embedding.invalid",
            "embedding",
            1024,
            "none",
            None,
            1,
        ),
        decision=DecisionRuntime(3, 3, 0.0, 1.0, 0.7),
        execution=ExecutionRuntime(
            backend="mindsdb",
            sql_api_url="http://mindsdb.invalid/api/sql/query",
            default_timeout_seconds=10,
            maximum_timeout_seconds=30,
            default_max_rows=1000,
            maximum_rows=10000,
            maximum_response_bytes=1000,
            audit_log_path=str(ROOT / "synthetic-audit.jsonl"),
        ),
        t2sql=T2SqlRuntime(
            model="t2sql-model" if configured else None,
            base_url="http://llm.invalid/v1" if configured else None,
            probe_timeout_seconds=8,
            total_timeout_seconds=60,
        ),
    )


def _state(**overrides):
    value = {
        "source_instance_id": SOURCE_ID,
        "engine": "tibero",
        "source_schema": "RWIS",
        "mindsdb_integration": "rwis_active",
        "mindsdb_catalog": "rwis_active",
        "source_name": "RWIS",
        "allowed_objects": ["RDITAG_TB", "RDF01HH_TB"],
        "allowed_schemas": ["RWIS"],
        "allowed_object_refs": [
            {
                "schema_name": "RWIS",
                "original_name": "RDITAG_TB",
                "subject_area": "master",
            },
            {
                "schema_name": "RWIS",
                "original_name": "RDF01HH_TB",
                "subject_area": "raw",
            },
        ],
    }
    value.update(overrides)
    return value


def _decision(**overrides) -> DecisionResponse:
    payload = dict(
        target="source",
        confidence=0.9,
        candidates=[
            DecisionCandidate(
                db="RWIS",
                schema_name="RWIS",
                table_name="RDITAG_TB",
                score=0.9,
                subject_area="master",
                matched_columns=[MatchedColumn(column_name="SUJ_NAME")],
            ),
            DecisionCandidate(
                db="RWIS",
                schema_name="RWIS",
                table_name="RDF01HH_TB",
                score=0.8,
                subject_area="raw",
                matched_columns=[MatchedColumn(column_name="AVG_VAL")],
            ),
        ],
        join_groups=[
            JoinGroup(
                members=[
                    TableKey(schema_name="RWIS", table_name="RDITAG_TB"),
                    TableKey(schema_name="RWIS", table_name="RDF01HH_TB"),
                ]
            )
        ],
        threshold_used={"minimum_similarity": 0.2},
        resolved_entities=[
            ResolvedEntity(
                mention="화성정수장",
                entity_type="facility",
                db="RWIS",
                schema_name="RWIS",
                table="RDITAG_TB",
                name_column="SUJ_NAME",
                code_column="SUJ_CODE",
            )
        ],
        suggested_probes=[],
        execution_context=ExecutionContext(
            backend="mindsdb",
            dialect="mysql",
            integration="rwis_active",
            catalog="rwis_active",
            schema_name="RWIS",
            qualification_pattern="{catalog}.{table}",
            identifier_quote="`",
            require_quoted_uppercase_identifiers=True,
            source_instance_id=SOURCE_ID,
            source_name="RWIS",
            allowed_objects=["RDITAG_TB", "RDF01HH_TB"],
        ),
        query_analysis=QueryAnalysis(status="complete", intent="avg turbidity"),
        query_plan=QueryPlan(
            completeness="complete",
            required_tables=[
                PlannedTable(
                    schema_name="RWIS",
                    table_name="RDITAG_TB",
                    role="태그 마스터",
                ),
                PlannedTable(
                    schema_name="RWIS",
                    table_name="RDF01HH_TB",
                    role="계측 팩트",
                ),
            ],
        ),
    )
    payload.update(overrides)
    return DecisionResponse(**payload)


async def _confirm_ok(question, payload, timeout_s):
    return {"accept": True, "missing": []}


async def _confirm_reject(question, payload, timeout_s):
    return {"accept": False, "missing": ["facility"]}


async def _generate_fact(question, payload, timeout_s):
    return FACT_SQL


async def _generate_fact_no_where(question, payload, timeout_s):
    return FACT_SQL_NO_WHERE


async def _ok_execute(**kwargs):
    caller = kwargs.get("caller")
    if caller == "t2sql_probe":
        return {
            "status": "ok",
            "columns": ["SUJ_NAME"],
            "rows": [["화성정수장"]],
        }
    return {"status": "ok", "columns": ["avg"], "rows": []}


class TimeoutDefaultTests(unittest.TestCase):
    def test_omitted_timeout_is_none_and_runtime_default_is_60(self):
        req = T2SqlRequest(query="화성정수장 평균 탁도")
        self.assertIsNone(req.timeout_s)
        self.assertEqual(
            T2SqlRuntime(model="m", base_url="http://x").total_timeout_seconds,
            60,
        )

    def test_blank_query_is_rejected(self):
        with self.assertRaises(Exception):
            T2SqlRequest(query="   ")


class ProbeAllowlistTests(unittest.TestCase):
    def test_probe_allowlist_excludes_fact_tables(self):
        decision = _decision()
        allowed = probe_allowlist(
            _state()["allowed_object_refs"],
            decision.resolved_entities,
        )
        self.assertIn(("rwis", "rditag_tb"), allowed)
        self.assertNotIn(("rwis", "rdf01hh_tb"), allowed)
        with self.assertRaises(GuardError):
            assert_sql_in_allowlist(FACT_SQL, allowed)
        store = {
            ("rwis", "rditag_tb"),
            ("rwis", "rdf01hh_tb"),
        }
        assert_sql_in_allowlist(FACT_SQL, store)

    def test_postgres_probe_keeps_store_identifier_case(self) -> None:
        entity = ResolvedEntity(
            mention="화성정수장",
            entity_type="facility",
            db="test_rwis",
            schema_name="rwis",
            table="rdisaup_tb",
            name_column="SUJ_NAME",
            code_column="SUJ_CODE",
        )
        sql = build_entity_probe_sql(
            "test_rwis",
            entity,
            "화성정수장",
            limit=20,
            fold_lower=False,
        )
        self.assertIsNotNone(sql)
        self.assertIn("`SUJ_NAME`", sql)
        self.assertIn("`SUJ_CODE`", sql)
        self.assertIn("`rdisaup_tb`", sql)
        self.assertIn("`test_rwis`", sql)

    def test_tibero_probe_keeps_quoted_uppercase(self) -> None:
        entity = _decision().resolved_entities[0]
        sql = build_entity_probe_sql(
            "RWIS",
            entity,
            "화성정수장",
            limit=20,
            fold_lower=False,
        )
        self.assertIsNotNone(sql)
        self.assertIn("`SUJ_NAME`", sql)
        self.assertIn("`RDITAG_TB`", sql)

    def test_resolved_code_does_not_need_live_probe(self) -> None:
        empty = _decision().resolved_entities[0]
        filled = empty.model_copy(
            update={"values": [ResolvedValue(code="SS", confidence=1.0)]}
        )
        self.assertTrue(entity_needs_live_probe(empty))
        self.assertFalse(entity_needs_live_probe(filled))


class T2SqlEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous = runtime_config._runtime
        runtime_config._runtime = _runtime()
        set_t2sql_decide(None)
        set_t2sql_execute(None)
        reset_t2sql_llm()

    def tearDown(self):
        set_t2sql_decide(None)
        set_t2sql_execute(None)
        reset_t2sql_llm()
        runtime_config._runtime = self.previous

    async def test_generated_filters_used_metadata_and_zero_rows(self):
        captured = {"decide_kwargs": None, "execute_callers": []}

        async def decide(repository, **kwargs):
            captured["decide_kwargs"] = kwargs
            return _decision()

        async def execute(**kwargs):
            captured["execute_callers"].append(kwargs.get("caller"))
            sql = kwargs.get("sql") or ""
            self.assertIn("LIMIT", sql.upper())
            return await _ok_execute(**kwargs)

        set_t2sql_decide(decide)
        set_t2sql_execute(execute)
        set_t2sql_llm(confirm=_confirm_ok, generate=_generate_fact)
        req = T2SqlRequest(
            query="화성정수장 평균 탁도",
            include_matched_columns=False,
            auto_resolve_entities=False,
        )
        result = await run_t2sql(FakeRepository(_state()), req)
        self.assertEqual(result.meta_version, META_VERSION)
        self.assertEqual(result.sql_status, "generated")
        self.assertIsNone(result.sql_reason)
        self.assertIsNone(result.sql_reason_code)
        self.assertEqual(_norm_sql(result.sql), FACT_SQL_PUBLIC)
        self.assertNotIn("LIMIT", result.sql.upper())
        names = {item.table_name for item in result.used_metadata.candidates}
        self.assertEqual(names, {"RDF01HH_TB"})
        self.assertEqual(result.used_metadata.candidates[0].matched_columns, [])
        self.assertIsNotNone(result.used_metadata.query_analysis)
        self.assertNotIn("target", result.model_dump())
        self.assertIn("t2sql_probe", captured["execute_callers"])
        self.assertIn("t2sql_validate", captured["execute_callers"])
        self.assertTrue(captured["decide_kwargs"]["include_matched_columns"])
        self.assertFalse(captured["decide_kwargs"]["auto_resolve_entities"])
        probe_sqls = result.probe_summary.probe_sqls
        self.assertTrue(probe_sqls)
        self.assertNotEqual(result.sql, probe_sqls[0])

    async def test_empty_month_fact_retries_day_grain(self):
        captured = {"overrides": [], "validate_sqls": []}
        resolved_entity = _decision().resolved_entities[0].model_copy(
            update={"values": [ResolvedValue(code="358", confidence=1.0)]}
        )

        def month_decision():
            return _decision(
                resolved_entities=[resolved_entity],
                query_plan=QueryPlan(
                    completeness="complete",
                    required_tables=[
                        PlannedTable(
                            schema_name="RWIS",
                            table_name="RDITAG_TB",
                            role="태그 마스터",
                        ),
                        PlannedTable(
                            schema_name="RWIS",
                            table_name="RDD01MM_TB",
                            role="월별 계측 팩트",
                        ),
                    ],
                ),
                candidates=[
                    DecisionCandidate(
                        db="RWIS",
                        schema_name="RWIS",
                        table_name="RDITAG_TB",
                        score=0.9,
                        subject_area="master",
                    ),
                    DecisionCandidate(
                        db="RWIS",
                        schema_name="RWIS",
                        table_name="RDD01MM_TB",
                        score=0.8,
                        subject_area="agg",
                    ),
                ],
            )

        def day_decision():
            return _decision(
                resolved_entities=[resolved_entity],
                query_plan=QueryPlan(
                    completeness="complete",
                    required_tables=[
                        PlannedTable(
                            schema_name="RWIS",
                            table_name="RDITAG_TB",
                            role="태그 마스터",
                        ),
                        PlannedTable(
                            schema_name="RWIS",
                            table_name="RDD01DD_TB",
                            role="일별 계측 팩트",
                        ),
                    ],
                ),
                candidates=[
                    DecisionCandidate(
                        db="RWIS",
                        schema_name="RWIS",
                        table_name="RDITAG_TB",
                        score=0.9,
                        subject_area="master",
                    ),
                    DecisionCandidate(
                        db="RWIS",
                        schema_name="RWIS",
                        table_name="RDD01DD_TB",
                        score=0.8,
                        subject_area="agg",
                    ),
                ],
            )

        async def decide(repository, **kwargs):
            captured["overrides"].append(kwargs.get("grain_override"))
            if kwargs.get("grain_override") == "day":
                return day_decision()
            return month_decision()

        async def generate(question, payload, timeout_s):
            plan = payload.get("query_plan") or {}
            names = [
                str(item.get("table_name") or "")
                for item in (plan.get("required_tables") or [])
            ]
            if "RDD01DD_TB" in names:
                return DAY_FACT_SQL
            return MONTH_FACT_SQL

        async def execute(**kwargs):
            sql = kwargs.get("sql") or ""
            if kwargs.get("caller") == "t2sql_validate":
                captured["validate_sqls"].append(sql)
                if "RDD01MM_TB" in sql.upper():
                    return {
                        "status": "ok",
                        "columns": ["avg"],
                        "rows": [],
                        "row_count": 0,
                    }
                return {
                    "status": "ok",
                    "columns": ["avg"],
                    "rows": [[1.2]],
                    "row_count": 1,
                }
            return await _ok_execute(**kwargs)

        set_t2sql_decide(decide)
        set_t2sql_execute(execute)
        set_t2sql_llm(confirm=_confirm_ok, generate=generate)
        result = await run_t2sql(
            FakeRepository(
                _state(
                    allowed_objects=["RDITAG_TB", "RDD01MM_TB", "RDD01DD_TB"],
                    allowed_object_refs=[
                        {
                            "schema_name": "RWIS",
                            "original_name": "RDITAG_TB",
                            "subject_area": "master",
                        },
                        {
                            "schema_name": "RWIS",
                            "original_name": "RDD01MM_TB",
                            "subject_area": "fact",
                        },
                        {
                            "schema_name": "RWIS",
                            "original_name": "RDD01DD_TB",
                            "subject_area": "fact",
                        },
                    ],
                )
            ),
            T2SqlRequest(query="단양정수장 2025년 8월 탁도 알려줘"),
        )
        self.assertEqual(result.sql_status, "generated")
        self.assertEqual(captured["overrides"], [None, "day"])
        self.assertEqual(len(captured["validate_sqls"]), 2)
        self.assertIn("RDD01DD_TB", result.sql or "")
        self.assertNotIn("RDD01MM_TB", result.sql or "")
        self.assertEqual(result.sql_reason, "월 팩트 0건으로 일 팩트를 재조회했습니다")

    async def test_generate_payload_uses_plan_not_candidates(self):
        captured: dict = {}

        async def decide(repository, **kwargs):
            return _decision()

        async def generate(question, payload, timeout_s):
            captured["payload"] = payload
            return await _generate_fact(question, payload, timeout_s)

        set_t2sql_decide(decide)
        set_t2sql_execute(_ok_execute)
        set_t2sql_llm(confirm=_confirm_ok, generate=generate)
        result = await run_t2sql(
            FakeRepository(_state()),
            T2SqlRequest(query="항목 조회"),
        )
        self.assertEqual(result.sql_status, "generated")
        self.assertNotIn("candidates", captured["payload"])
        self.assertIn("query_plan", captured["payload"])
        self.assertIn("table_notes", captured["payload"])
        for note in captured["payload"]["table_notes"]:
            self.assertNotIn("default_date_column", note)
            self.assertNotIn("table_type", note)

    async def test_skips_probe_when_code_already_resolved(self):
        captured = {"execute_callers": []}

        async def decide(repository, **kwargs):
            entity = _decision().resolved_entities[0].model_copy(
                update={"values": [ResolvedValue(code="617", confidence=1.0)]}
            )
            return _decision(resolved_entities=[entity])

        async def execute(**kwargs):
            captured["execute_callers"].append(kwargs.get("caller"))
            return await _ok_execute(**kwargs)

        set_t2sql_decide(decide)
        set_t2sql_execute(execute)
        set_t2sql_llm(confirm=_confirm_ok, generate=_generate_fact)
        result = await run_t2sql(
            FakeRepository(_state()),
            T2SqlRequest(query="화성정수장 평균 탁도"),
        )
        self.assertEqual(result.sql_status, "generated")
        self.assertNotIn("t2sql_probe", captured["execute_callers"])
        self.assertIn("t2sql_validate", captured["execute_callers"])
        self.assertEqual(result.probe_summary.probes_run, 0)
        self.assertEqual(result.probe_summary.resolution_status, "skipped")

    async def test_probe_db_error_does_not_abort_generation(self):
        async def decide(repository, **kwargs):
            return _decision()

        async def execute(**kwargs):
            if kwargs.get("caller") == "t2sql_probe":
                return {
                    "status": "db_error",
                    "error": 'relation "rdibyun_tb" does not exist',
                }
            return await _ok_execute(**kwargs)

        set_t2sql_decide(decide)
        set_t2sql_execute(execute)
        set_t2sql_llm(confirm=_confirm_ok, generate=_generate_fact)
        result = await run_t2sql(
            FakeRepository(_state()),
            T2SqlRequest(query="화성정수장 평균 탁도"),
        )
        self.assertEqual(result.sql_status, "generated")
        self.assertEqual(_norm_sql(result.sql), FACT_SQL_PUBLIC)
        self.assertEqual(result.probe_summary.probes_failed, 1)
        self.assertIsNotNone(result.used_metadata.query_plan)

    async def test_plan_incomplete(self):
        async def decide(repository, **kwargs):
            return _decision(
                query_analysis=QueryAnalysis(status="degraded", intent="x"),
                query_plan=QueryPlan(completeness="failed"),
            )

        set_t2sql_decide(decide)
        set_t2sql_llm(confirm=_confirm_ok, generate=_generate_fact)
        result = await run_t2sql(
            FakeRepository(_state()),
            T2SqlRequest(query="화성정수장 평균 탁도"),
        )
        self.assertEqual(result.sql_status, "failed")
        self.assertEqual(result.sql_reason_code, "PLAN_INCOMPLETE")
        self.assertIsNone(result.sql)

    async def test_generates_when_analysis_degraded_if_plan_has_tables(self):
        async def decide(repository, **kwargs):
            return _decision(
                query_analysis=QueryAnalysis(status="degraded", intent="x"),
                query_plan=QueryPlan(
                    completeness="partial",
                    required_tables=[
                        PlannedTable(
                            schema_name="RWIS",
                            table_name="RDF01HH_TB",
                            role="계측 팩트",
                        )
                    ],
                    filters=[
                        PlannedFilter(
                            meaning="코드",
                            column="RWIS.RDITAG_TB.SUJ_CODE",
                            operator="EQ",
                            value="617",
                            resolution_status="resolved",
                            confidence=1.0,
                        )
                    ],
                ),
            )

        set_t2sql_decide(decide)
        set_t2sql_execute(_ok_execute)
        set_t2sql_llm(confirm=_confirm_ok, generate=_generate_fact)
        result = await run_t2sql(
            FakeRepository(_state()),
            T2SqlRequest(query="화성정수장 평균 탁도"),
        )
        self.assertEqual(result.sql_status, "generated")
        self.assertEqual(_norm_sql(result.sql), FACT_SQL_PUBLIC)

    async def test_confirm_reject(self):
        async def decide(repository, **kwargs):
            return _decision()

        set_t2sql_decide(decide)
        set_t2sql_execute(_ok_execute)
        set_t2sql_llm(confirm=_confirm_reject, generate=_generate_fact)
        result = await run_t2sql(
            FakeRepository(_state()),
            T2SqlRequest(query="화성정수장 평균 탁도"),
        )
        self.assertEqual(result.sql_status, "failed")
        self.assertEqual(result.sql_reason_code, "ENTITY_UNRESOLVED")
        self.assertIsNone(result.sql)

    async def test_confirm_reject_is_reconciled_when_plan_has_codes(self):
        async def decide(repository, **kwargs):
            entity = _decision().resolved_entities[0].model_copy(
                update={
                    "values": [
                        ResolvedValue(code="617", confidence=1.0),
                        ResolvedValue(code="999", confidence=1.0),
                    ]
                }
            )
            return _decision(
                resolved_entities=[entity],
                query_plan=QueryPlan(
                    completeness="complete",
                    filters=[
                        PlannedFilter(
                            meaning="사업장 명칭",
                            column="RWIS.RDITAG_TB.SUJ_CODE",
                            operator="EQ",
                            value="617",
                            resolution_status="resolved",
                            confidence=1.0,
                        )
                    ],
                ),
            )

        captured = {"confirm_called": False}

        async def confirm_must_not_run(question, payload, timeout_s):
            captured["confirm_called"] = True
            return {"accept": False, "missing": ["사업장 코드", "기간"]}

        set_t2sql_decide(decide)
        set_t2sql_execute(_ok_execute)
        set_t2sql_llm(confirm=confirm_must_not_run, generate=_generate_fact)
        result = await run_t2sql(
            FakeRepository(_state()),
            T2SqlRequest(query="화성정수장 평균 탁도"),
        )
        self.assertEqual(result.sql_status, "generated")
        self.assertEqual(_norm_sql(result.sql), FACT_SQL_PUBLIC)
        self.assertFalse(captured["confirm_called"])

    async def test_guard_rejected_sql_is_null(self):
        async def decide(repository, **kwargs):
            return _decision()

        set_t2sql_decide(decide)
        set_t2sql_execute(_ok_execute)
        set_t2sql_llm(confirm=_confirm_ok, generate=_generate_fact_no_where)
        result = await run_t2sql(
            FakeRepository(_state()),
            T2SqlRequest(query="화성정수장 평균 탁도"),
        )
        self.assertEqual(result.sql_status, "validation_failed")
        self.assertEqual(result.sql_reason_code, "GUARD_REJECTED")
        self.assertEqual(_norm_sql(result.sql), FACT_SQL_NO_WHERE_PUBLIC)

    async def test_validate_missing_relation_is_not_accepted(self):
        async def decide(repository, **kwargs):
            return _decision()

        async def execute(**kwargs):
            if kwargs.get("caller") == "t2sql_validate":
                return {
                    "status": "db_error",
                    "error": 'relation "rditag_tb" does not exist',
                }
            return await _ok_execute(**kwargs)

        set_t2sql_decide(decide)
        set_t2sql_execute(execute)
        set_t2sql_llm(confirm=_confirm_ok, generate=_generate_fact)
        result = await run_t2sql(
            FakeRepository(_state()),
            T2SqlRequest(query="화성정수장 평균 탁도"),
        )
        self.assertEqual(result.sql_status, "validation_failed")
        self.assertIn("does not exist", result.sql_reason or "")

    async def test_unplanned_table_is_rejected(self):
        async def decide(repository, **kwargs):
            return _decision(
                query_plan=QueryPlan(
                    completeness="complete",
                    required_tables=[
                        PlannedTable(
                            schema_name="RWIS",
                            table_name="RDITAG_TB",
                            role="태그 마스터",
                        ),
                        PlannedTable(
                            schema_name="RWIS",
                            table_name="RDF01HH_TB",
                            role="계측 팩트",
                        ),
                    ],
                )
            )

        async def generate_extra(question, payload, timeout_s):
            return (
                FACT_SQL
                + " JOIN `RWIS`.`RWIS`.`RAW_SHARD_TB` ON 1=1"
            )

        set_t2sql_decide(decide)
        set_t2sql_execute(_ok_execute)
        set_t2sql_llm(confirm=_confirm_ok, generate=generate_extra)
        repo_state = _state()
        repo_state["allowed_objects"] = [
            "RDITAG_TB",
            "RDF01HH_TB",
            "RAW_SHARD_TB",
        ]
        repo_state["allowed_object_refs"] = [
            *repo_state["allowed_object_refs"],
            {
                "schema_name": "RWIS",
                "original_name": "RAW_SHARD_TB",
                "subject_area": "raw",
            },
        ]
        result = await run_t2sql(
            FakeRepository(repo_state),
            T2SqlRequest(query="화성정수장 평균 탁도"),
        )
        self.assertEqual(result.sql_status, "validation_failed")
        self.assertIn("계획에 없는 표", result.sql_reason or "")

    async def test_validate_timeout(self):
        async def decide(repository, **kwargs):
            return _decision()

        async def execute(**kwargs):
            if kwargs.get("caller") == "t2sql_validate":
                return {"status": "timeout", "error": "statement timeout"}
            return await _ok_execute(**kwargs)

        set_t2sql_decide(decide)
        set_t2sql_execute(execute)
        set_t2sql_llm(confirm=_confirm_ok, generate=_generate_fact)
        result = await run_t2sql(
            FakeRepository(_state()),
            T2SqlRequest(query="화성정수장 평균 탁도"),
        )
        self.assertEqual(result.sql_status, "failed")
        self.assertEqual(result.sql_reason_code, "TIMEOUT")
        self.assertEqual(_norm_sql(result.sql), FACT_SQL_PUBLIC)

    async def test_upstream_unconfigured(self):
        runtime_config._runtime = _runtime(configured=False)
        result = await run_t2sql(
            FakeRepository(_state()),
            T2SqlRequest(query="화성정수장 평균 탁도"),
        )
        self.assertEqual(result.sql_status, "failed")
        self.assertEqual(result.sql_reason_code, "UPSTREAM_UNAVAILABLE")
        self.assertIsNone(result.sql)
        self.assertEqual(result.meta_version, "1.0")


class T2SqlHttpTests(unittest.TestCase):
    def setUp(self):
        self.previous = runtime_config._runtime
        runtime_config._runtime = _runtime()
        set_t2sql_decide(None)
        set_t2sql_execute(None)
        reset_t2sql_llm()
        self.app = FastAPI()
        self.app.include_router(t2sql_router.router)
        self.repo = FakeRepository(_state())
        self.patcher = patch(
            "app.routers.t2sql.get_metadata_repository",
            return_value=self.repo,
        )
        self.patcher.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self.patcher.stop()
        set_t2sql_decide(None)
        set_t2sql_execute(None)
        reset_t2sql_llm()
        runtime_config._runtime = self.previous

    def test_blank_query_422(self):
        response = self.client.post("/t2sql", json={"query": "   "})
        self.assertEqual(response.status_code, 422)

    def test_openapi_has_t2sql_and_own_request_schema(self):
        spec = self.client.app.openapi()
        self.assertIn("/t2sql", spec["paths"])
        schemas = spec["components"]["schemas"]
        self.assertIn("T2SqlRequest", schemas)
        self.assertNotEqual(
            schemas["T2SqlRequest"],
            schemas.get("DecisionRequest"),
        )
        timeout = schemas["T2SqlRequest"]["properties"]["timeout_s"]
        dumped = str(timeout)
        self.assertIn("60", dumped)

    def test_http_generated(self):
        async def decide(repository, **kwargs):
            return _decision()

        set_t2sql_decide(decide)
        set_t2sql_execute(_ok_execute)
        set_t2sql_llm(confirm=_confirm_ok, generate=_generate_fact)
        response = self.client.post(
            "/t2sql",
            json={"query": "화성정수장 평균 탁도"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["meta_version"], "1.0")
        self.assertEqual(body["sql_status"], "generated")
        self.assertEqual(_norm_sql(body["sql"]), FACT_SQL_PUBLIC)
        self.assertIsNone(body["sql_reason_code"])


class MainAppRouteTests(unittest.TestCase):
    def test_main_app_exposes_t2sql_path(self):
        from app.main import app

        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertIn("/t2sql", paths)
        self.assertEqual(META_VERSION, "1.0")


class LoadRuntimeT2SqlTests(unittest.TestCase):
    def test_missing_t2sql_block_uses_code_defaults(self):
        previous = os.environ.get("T2SQL_LLM_MODEL")
        os.environ["METADATA_PG_PASSWORD"] = "pw"
        os.environ.pop("T2SQL_LLM_MODEL", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "ok.yaml"
                path.write_text(
                    """
robo_meta_api:
  api_host: "0.0.0.0"
  api_port: 8100
  metadata_backend: postgres
  metadata_store:
    host: h
    port: 5432
    database: d
    schema: public
    user: u
    password_ref: {provider: process_env, env_key: METADATA_PG_PASSWORD}
  embedding:
    base_url: http://x
    model: m
    dimensions: 8
    auth_mode: none
    timeout_seconds: 1
  decision:
    table_top_k: 1
    column_top_m: 1
    minimum_similarity: 0.0
    verified_join_confidence: 1.0
    convention_join_confidence: 0.7
  execution:
    backend: mindsdb
    sql_api_url: http://mindsdb
    default_timeout_seconds: 1
    maximum_timeout_seconds: 30
    default_max_rows: 1
    maximum_rows: 20
    maximum_response_bytes: 10
    audit_log_path: ./a.jsonl
""".strip(),
                    encoding="utf-8",
                )
                runtime = load_runtime(path)
                self.assertIsNotNone(runtime.t2sql)
                self.assertEqual(runtime.t2sql.total_timeout_seconds, 60)
                self.assertFalse(runtime.t2sql.configured())
        finally:
            if previous is None:
                os.environ.pop("T2SQL_LLM_MODEL", None)
            else:
                os.environ["T2SQL_LLM_MODEL"] = previous


if __name__ == "__main__":
    unittest.main()
