"""POST /t2sql orchestrator."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Awaitable, Callable
from uuid import uuid4

from ...runtime_config import get_runtime
from ...schemas import (
    DecisionResponse,
    ExecutionContext,
    ProbeSummary,
    QueryPlan,
    SqlReasonCode,
    SqlStatus,
    T2SqlRequest,
    T2SqlResponse,
    T2SqlUsedMetadata,
)
from ..decision_postgres.decide import decide as decide_postgres
from ..decision_postgres.period import parse_korean_period
from ..decision_postgres.store_first import (
    is_fact_unresolved_reason,
    query_requests_fact,
)
from ..execution_context_resolver import (
    ExecutionBindingError,
    ResolvedExecutionContext,
    resolve_execution_context,
)
from ..query_runner_mindsdb import execute as mindsdb_execute
from ..sql_guard import GuardError
from ..sql_source_qualify import compact_public_sql, extract_sql_table_refs
from .confirm import (
    align_entities_to_plan,
    fact_left_unresolved,
    reconcile_confirm,
    resolved_plan_values,
    should_skip_confirm,
)
from .join_on import missing_approved_on_predicates
from .llm import confirm_intent, generate_sql
from .probe import (
    assert_sql_in_allowlist,
    build_entity_probe_sql,
    entity_needs_live_probe,
    probe_allowlist,
    with_limit,
)
from .used_meta import empty_used_metadata, filter_used_metadata, used_table_keys

ExecuteFn = Callable[..., Awaitable[dict[str, Any]]]
DecideFn = Callable[..., Awaitable[DecisionResponse]]
logger = logging.getLogger(__name__)

_execute_override: ExecuteFn | None = None
_decide_override: DecideFn | None = None


def set_t2sql_execute(fn: ExecuteFn | None) -> None:
    global _execute_override
    _execute_override = fn


def set_t2sql_decide(fn: DecideFn | None) -> None:
    global _decide_override
    _decide_override = fn


class _Budget:
    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + max(0.01, seconds)

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0


def _empty(
    *,
    generation_id: str,
    started: float,
    status: SqlStatus,
    code: SqlReasonCode | None,
    reason: str | None,
    used: T2SqlUsedMetadata | None = None,
    probe: ProbeSummary | None = None,
) -> T2SqlResponse:
    return T2SqlResponse(
        generation_id=generation_id,
        sql=None,
        sql_status=status,
        sql_reason=reason,
        sql_reason_code=code,
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        used_metadata=used or empty_used_metadata(),
        probe_summary=probe or ProbeSummary(),
    )


def _public_sql(
    sql_text: str | None,
    resolved: ResolvedExecutionContext | None,
) -> str | None:
    if not sql_text:
        return sql_text
    if resolved is None:
        return sql_text
    try:
        return compact_public_sql(sql_text, execution_context=resolved)
    except Exception:
        return sql_text


def _strip_sql_fence(text: str) -> str:
    """Remove markdown fences only. Do not strip SQL identifier backticks."""

    stripped = (text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def quote_resolved_code_literals(sql: str, plan: QueryPlan | None) -> str:
    """Resolved store codes are text. Do not leave them as bare integers."""

    if not sql or plan is None:
        return sql
    codes: list[str] = []
    for filt in plan.filters:
        if filt.resolution_status != "resolved":
            continue
        if filt.operator not in {"EQ", "IN"}:
            continue
        for part in str(filt.value or "").split(","):
            code = part.strip()
            if code.isdigit():
                codes.append(code)
    rewritten = sql
    for code in sorted(set(codes), key=len, reverse=True):
        rewritten = re.sub(
            rf"(=\s*)(?!['\"]){re.escape(code)}\b",
            rf"\1'{code}'",
            rewritten,
        )
        rewritten = re.sub(
            rf"(\bIN\s*\((?:[^)]*?))(?<!['\d]){re.escape(code)}\b",
            rf"\1'{code}'",
            rewritten,
        )
    return rewritten


def _t2sql_settings():
    runtime = get_runtime()
    return runtime.t2sql


async def _execute(**kwargs: Any) -> dict[str, Any]:
    fn = _execute_override or mindsdb_execute
    return await fn(**kwargs)


async def _decide(repository: Any, req: T2SqlRequest) -> DecisionResponse:
    fn = _decide_override or decide_postgres
    return await fn(
        repository,
        query=req.query,
        include_matched_columns=True,
        column_top_m=req.column_top_m,
        table_limit=req.table_limit,
        auto_resolve_entities=req.auto_resolve_entities,
    )


async def _source_id(repository: Any, decision: DecisionResponse) -> str | None:
    context = decision.execution_context
    if context and context.source_instance_id:
        return context.source_instance_id
    name = None
    if context and context.source_name:
        name = context.source_name
    if not name:
        return None
    finder = getattr(repository, "find_profile_ids_by_source_name", None)
    if finder is None:
        return None
    matches = await finder(name)
    if len(matches) != 1:
        return None
    return matches[0]


def _public_ec_from_resolved(resolved: Any) -> ExecutionContext | None:
    try:
        if not resolved.source_name:
            return None
        return ExecutionContext(**resolved.public_dict())
    except Exception:
        return None


def _plan_table_names(plan: Any) -> set[str]:
    names: set[str] = set()
    if plan is None:
        return names
    for table in getattr(plan, "required_tables", []) or []:
        names.add(str(getattr(table, "table_name", "") or "").lower())
    for table in getattr(plan, "bridge_tables", []) or []:
        names.add(str(getattr(table, "table_name", "") or "").lower())
    for path in getattr(plan, "join_paths", []) or []:
        names.add(str(path.from_table.table_name or "").lower())
        names.add(str(path.to_table.table_name or "").lower())
        for bridge in path.bridge_tables or []:
            names.add(str(bridge.table_name or "").lower())
    return {name for name in names if name}


def _plan_table_notes(decision: DecisionResponse, plan: Any) -> list[dict[str, Any]]:
    names = {name.lower() for name in _plan_table_names(plan)}
    notes: list[dict[str, Any]] = []
    for candidate in decision.candidates:
        if str(candidate.table_name or "").lower() not in names:
            continue
        notes.append(
            {
                "table_name": candidate.table_name,
                "logical_name": candidate.logical_name,
                "description": candidate.description,
            }
        )
    return notes


def _unplanned_sql_tables(sql: str, plan: Any) -> list[str]:
    allowed = _plan_table_names(plan)
    if not allowed:
        return []
    extra: list[str] = []
    for ref in extract_sql_table_refs(sql):
        if ref.table_name.lower() not in allowed:
            extra.append(ref.table_name)
    return extra


def _uses_unfiltered_fact(sql: str, probe_allowed: set[tuple[str, str]]) -> bool:
    if re.search(r"\bWHERE\b", sql, re.I):
        return False
    for ref in extract_sql_table_refs(sql):
        schema = (ref.schema_name or "").lower()
        table = ref.table_name.lower()
        if schema:
            if (schema, table) not in probe_allowed:
                return True
            continue
        if not any(item[1] == table for item in probe_allowed):
            return True
    return False


async def run_t2sql(repository: Any, req: T2SqlRequest) -> T2SqlResponse:
    generation_id = str(uuid4())
    started = time.perf_counter()
    settings = _t2sql_settings()
    if settings is None or not settings.configured():
        return _empty(
            generation_id=generation_id,
            started=started,
            status="failed",
            code="UPSTREAM_UNAVAILABLE",
            reason="T2SQL 모델이 설정되지 않았습니다",
        )

    budget = _Budget(float(req.timeout_s or settings.total_timeout_seconds))
    runtime = get_runtime()
    max_stmt = runtime.execution.maximum_timeout_seconds

    def stmt_timeout(step_cap: int) -> int:
        remaining = int(budget.remaining())
        return max(1, min(remaining, step_cap, max_stmt))

    def llm_timeout() -> float:
        return max(0.1, min(budget.remaining(), float(settings.generate_timeout_seconds)))

    if budget.expired():
        return _empty(
            generation_id=generation_id,
            started=started,
            status="failed",
            code="TIMEOUT",
            reason="파이프라인 시간 초과",
        )

    decision = await _decide(repository, req)
    if budget.expired():
        return _empty(
            generation_id=generation_id,
            started=started,
            status="failed",
            code="TIMEOUT",
            reason="파이프라인 시간 초과",
        )

    analysis = decision.query_analysis
    plan = decision.query_plan
    if plan is not None and "맞는 메타데이터가 없다" in (
        plan.unresolved_requirements or []
    ):
        return _empty(
            generation_id=generation_id,
            started=started,
            status="failed",
            code="NO_METADATA",
            reason="맞는 메타데이터가 없다",
            used=T2SqlUsedMetadata(
                query_analysis=analysis,
                query_plan=plan,
            ),
        )
    mapping_hints = []
    if plan is not None:
        mapping_hints.extend(
            {"logical_name": table.role, "column_fqn": ""}
            for table in plan.required_tables
        )
        mapping_hints.extend(
            {"logical_name": "", "column_fqn": item.column or ""}
            for item in plan.filters
        )
    needs_fact = query_requests_fact(
        req.query,
        parse_korean_period(req.query),
        analysis,
        mapping_hints,
    )
    if fact_left_unresolved(plan) and needs_fact:
        reason = next(
            (
                str(item)
                for item in (plan.unresolved_requirements or [])
                if "팩트" in str(item)
            ),
            "팩트 표 미선정",
        )
        return _empty(
            generation_id=generation_id,
            started=started,
            status="failed",
            code="PLAN_INCOMPLETE",
            reason=reason,
            used=T2SqlUsedMetadata(
                query_analysis=analysis,
                query_plan=plan,
                candidates=decision.candidates,
                join_groups=decision.join_groups,
                resolved_entities=decision.resolved_entities,
                execution_context=decision.execution_context,
            ),
        )
    if plan is not None and not needs_fact:
        plan = plan.model_copy(
            update={
                "unresolved_requirements": [
                    item
                    for item in (plan.unresolved_requirements or [])
                    if not is_fact_unresolved_reason(str(item))
                ]
            }
        )
    has_tables = bool(plan and plan.required_tables)
    has_resolved = bool(plan and resolved_plan_values(plan))
    if plan is None or not (has_tables or has_resolved):
        return _empty(
            generation_id=generation_id,
            started=started,
            status="failed",
            code="PLAN_INCOMPLETE",
            reason="query_plan에 생성할 표나 resolved 필터가 없습니다",
            used=T2SqlUsedMetadata(
                query_analysis=analysis,
                query_plan=plan,
                candidates=decision.candidates,
                join_groups=decision.join_groups,
                resolved_entities=decision.resolved_entities,
                execution_context=decision.execution_context,
            ),
        )
    if any(group.cross_db for group in decision.join_groups):
        return _empty(
            generation_id=generation_id,
            started=started,
            status="failed",
            code="CROSS_DB",
            reason="이종 소스 JOIN은 허용되지 않습니다",
            used=T2SqlUsedMetadata(
                query_analysis=analysis,
                query_plan=plan,
                candidates=decision.candidates,
                join_groups=decision.join_groups,
                resolved_entities=decision.resolved_entities,
            ),
        )

    source_id = await _source_id(repository, decision)
    if not source_id:
        return _empty(
            generation_id=generation_id,
            started=started,
            status="failed",
            code="PLAN_INCOMPLETE",
            reason="source_instance_id를 결정할 수 없습니다",
        )

    scope = await repository.execution_source_scope(source_id)
    if scope is None:
        return _empty(
            generation_id=generation_id,
            started=started,
            status="failed",
            code="PLAN_INCOMPLETE",
            reason="Metadata Store에 datasource가 없습니다",
        )

    probe_allowed = probe_allowlist(
        list(scope.get("allowed_object_refs") or []),
        decision.resolved_entities,
    )
    source_name = str(scope.get("source_name") or "").strip()
    probe = ProbeSummary()
    probe_started = time.perf_counter()
    probe_rows: list[dict[str, Any]] = []
    try:
        resolved = await resolve_execution_context(
            repository,
            source_instance_id=source_id,
        )
    except ExecutionBindingError as exc:
        return _empty(
            generation_id=generation_id,
            started=started,
            status="failed",
            code="PLAN_INCOMPLETE",
            reason=str(exc),
            probe=probe,
        )

    used_after_decide = T2SqlUsedMetadata(
        query_analysis=analysis,
        query_plan=plan,
        candidates=decision.candidates,
        join_groups=decision.join_groups,
        resolved_entities=decision.resolved_entities,
        execution_context=decision.execution_context,
    )
    entities = decision.resolved_entities[: settings.max_probe_steps]
    for entity in entities:
        if budget.expired():
            probe.elapsed_ms = round((time.perf_counter() - probe_started) * 1000.0, 1)
            return _empty(
                generation_id=generation_id,
                started=started,
                status="failed",
                code="TIMEOUT",
                reason="파이프라인 시간 초과",
                probe=probe,
                used=used_after_decide,
            )
        if not entity_needs_live_probe(entity):
            continue
        sql = build_entity_probe_sql(
            source_name or str(resolved.source_name or ""),
            entity,
            entity.mention or req.query,
            limit=settings.probe_row_limit,
            fold_lower=False,
        )
        if not sql:
            continue
        try:
            assert_sql_in_allowlist(sql, probe_allowed)
        except GuardError:
            continue
        probe.probes_run += 1
        probe.probe_sqls.append(sql)
        result = await _execute(
            sql=sql,
            timeout_s=stmt_timeout(settings.probe_timeout_seconds),
            max_rows=settings.probe_row_limit,
            caller="t2sql_probe",
            execution_context=resolved,
        )
        if result.get("status") == "timeout":
            probe.probes_failed += 1
            probe.last_error = str(result.get("error") or "timeout")
            probe.elapsed_ms = round((time.perf_counter() - probe_started) * 1000.0, 1)
            return _empty(
                generation_id=generation_id,
                started=started,
                status="failed",
                code="TIMEOUT",
                reason="probe timeout",
                probe=probe,
                used=used_after_decide,
            )
        if result.get("status") not in {"ok"}:
            probe.probes_failed += 1
            probe.last_error = str(result.get("error") or result.get("status"))
            continue
        probe.probes_ok += 1
        probe_rows.append(
            {
                "entity": entity.model_dump(),
                "columns": result.get("columns") or [],
                "rows": (result.get("rows") or [])[: settings.probe_row_limit],
            }
        )
    probe.elapsed_ms = round((time.perf_counter() - probe_started) * 1000.0, 1)
    probe.resolution_status = (
        "complete"
        if probe.probes_ok
        else ("skipped" if probe.probes_run == 0 else "failed")
    )

    if budget.expired():
        return _empty(
            generation_id=generation_id,
            started=started,
            status="failed",
            code="TIMEOUT",
            reason="파이프라인 시간 초과",
            probe=probe,
        )

    aligned_entities = align_entities_to_plan(decision.resolved_entities, plan)
    if should_skip_confirm(plan):
        confirm = {"accept": True, "missing": []}
        logger.warning(
            "t2sql confirm skipped generation_id=%s plan=complete filters=%s",
            generation_id,
            resolved_plan_values(plan),
        )
    else:
        try:
            raw_confirm = await confirm_intent(
                req.query,
                {
                    "query_analysis": analysis.model_dump(),
                    "query_plan": plan.model_dump(by_alias=True) if plan else None,
                    "glossary_routes": [
                        item.model_dump() for item in decision.glossary_routes
                    ],
                    "resolved_entities": [
                        item.model_dump() for item in aligned_entities
                    ],
                    "probe_rows": probe_rows,
                },
                llm_timeout(),
            )
        except Exception as exc:
            return _empty(
                generation_id=generation_id,
                started=started,
                status="failed",
                code="UPSTREAM_UNAVAILABLE",
                reason=f"confirm LLM 실패: {exc}",
                probe=probe,
                used=used_after_decide,
            )
        confirm = reconcile_confirm(raw_confirm, plan=plan, analysis=analysis)
        logger.warning(
            "t2sql confirm generation_id=%s raw=%s reconciled=%s",
            generation_id,
            raw_confirm,
            confirm,
        )
    if not bool(confirm.get("accept")):
        return _empty(
            generation_id=generation_id,
            started=started,
            status="failed",
            code="ENTITY_UNRESOLVED",
            reason="의도 확인이 거부되었습니다: "
            + ",".join(str(item) for item in (confirm.get("missing") or [])),
            probe=probe,
            used=T2SqlUsedMetadata(
                query_analysis=analysis,
                query_plan=plan,
                candidates=decision.candidates,
                join_groups=decision.join_groups,
                resolved_entities=aligned_entities,
                execution_context=decision.execution_context,
            ),
        )

    sql_text = ""
    last_guard: str | None = None
    attempts = 1 + max(0, settings.max_generate_retries)
    store_allowed = {
        (
            str(item.get("schema_name") or "").lower(),
            str(item.get("original_name") or "").lower(),
        )
        for item in (scope.get("allowed_object_refs") or [])
        if item.get("schema_name") and item.get("original_name")
    }
    for _ in range(attempts):
        if budget.expired():
            return _empty(
                generation_id=generation_id,
                started=started,
                status="failed",
                code="TIMEOUT",
                reason="파이프라인 시간 초과",
                probe=probe,
            )
        try:
            sql_text = await generate_sql(
                req.query,
                {
                    "query_analysis": analysis.model_dump(),
                    "query_plan": plan.model_dump(by_alias=True) if plan else None,
                    "glossary_routes": [
                        item.model_dump() for item in decision.glossary_routes
                    ],
                    "resolved_entities": [
                        item.model_dump() for item in aligned_entities
                    ],
                    "probe_rows": probe_rows,
                    "source_name": source_name or resolved.source_name,
                    "table_notes": _plan_table_notes(decision, plan),
                },
                llm_timeout(),
            )
        except Exception as exc:
            return _empty(
                generation_id=generation_id,
                started=started,
                status="failed",
                code="UPSTREAM_UNAVAILABLE",
                reason=f"generate LLM 실패: {exc}",
                probe=probe,
            )
        sql_text = _strip_sql_fence(sql_text)
        sql_text = quote_resolved_code_literals(sql_text, plan)
        if sql_text.upper().startswith("NO_SQL"):
            return _empty(
                generation_id=generation_id,
                started=started,
                status="failed",
                code="GENERATION_FAILED",
                reason=sql_text,
                probe=probe,
            )
        if not sql_text:
            last_guard = "빈 SQL"
            continue
        try:
            assert_sql_in_allowlist(sql_text, store_allowed)
            if _uses_unfiltered_fact(sql_text, probe_allowed):
                raise GuardError("fact 테이블은 WHERE 필터가 필요합니다")
            missing_on = missing_approved_on_predicates(
                sql_text,
                plan.join_paths if plan else [],
            )
            if missing_on:
                raise GuardError("승인 JOIN ON이 빠짐: " + ",".join(missing_on))
            extra_tables = _unplanned_sql_tables(sql_text, plan)
            if extra_tables:
                raise GuardError("계획에 없는 표: " + ",".join(extra_tables))
            used_keys = used_table_keys(sql_text)
            validate_sql = with_limit(sql_text, settings.validate_max_rows)
            requested = sorted({table for _, table in used_keys if table})
            resolved_validate = await resolve_execution_context(
                repository,
                source_instance_id=source_id,
                requested_objects=requested or None,
            )
            result = await _execute(
                sql=validate_sql,
                timeout_s=stmt_timeout(max_stmt),
                max_rows=settings.validate_max_rows,
                caller="t2sql_validate",
                execution_context=resolved_validate,
            )
        except GuardError as exc:
            last_guard = str(exc)
            continue
        except ExecutionBindingError as exc:
            last_guard = str(exc)
            continue
        if result.get("status") == "timeout":
            return T2SqlResponse(
                generation_id=generation_id,
                sql=_public_sql(sql_text, resolved) if sql_text else None,
                sql_status="failed",
                sql_reason=str(result.get("error") or "validate timeout"),
                sql_reason_code="TIMEOUT",
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
                used_metadata=used_after_decide,
                probe_summary=probe,
            )
        if result.get("status") == "rejected":
            last_guard = str(result.get("error") or "rejected")
            continue
        if result.get("status") in {"error", "db_error"}:
            last_guard = str(result.get("error") or result.get("status"))
            logger.warning(
                "t2sql validate db_error generation_id=%s sql=%s error=%s",
                generation_id,
                sql_text,
                last_guard,
            )
            continue
        public_ec = _public_ec_from_resolved(resolved_validate)
        used = filter_used_metadata(
            decision,
            sql_text,
            include_matched_columns=req.include_matched_columns,
            execution_context=public_ec,
        )
        return T2SqlResponse(
            generation_id=generation_id,
            sql=_public_sql(sql_text, resolved),
            sql_status="generated",
            sql_reason=None,
            sql_reason_code=None,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
            used_metadata=used,
            probe_summary=probe,
        )

    logger.warning(
        "t2sql failed generation_id=%s status=%s reason=%s sql=%s",
        generation_id,
        "validation_failed" if last_guard else "failed",
        last_guard,
        sql_text or None,
    )
    return T2SqlResponse(
        generation_id=generation_id,
        sql=_public_sql(sql_text, resolved) if sql_text else None,
        sql_status="validation_failed" if last_guard else "failed",
        sql_reason=last_guard or "SQL을 생성하지 못했습니다",
        sql_reason_code="GUARD_REJECTED" if last_guard else "GENERATION_FAILED",
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
        used_metadata=used_after_decide,
        probe_summary=probe,
    )
