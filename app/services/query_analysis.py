"""Backend-independent natural-language requirement analysis for v1 decisions."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from openai import AsyncOpenAI

from ..runtime_config import get_runtime
from ..schemas import (
    FilterRequirement,
    QueryAnalysis,
    SchemaRoleRequirement,
)
from .decision_postgres.grain import _asks_series
from .meaning_slots import (
    ALLOWED_PROCEDURES,
    extremum_function_from_text,
    is_answer_axis_text,
    looks_physical_name,
    measure_item_surface,
)

logger = logging.getLogger(__name__)


QUERY_ANALYSIS_PROMPT = """\
당신은 Text-to-SQL의 의미 분해기다.
스토어 후보·물리 테이블명·컬럼명·코드값을 쓰지 마라.
질문에만 근거해 「답하려면 무엇을 확보해야 하는가」를 적는다.
질문에 없는 대상·기간은 빈 문자열이다. 빈 칸은 정상이다.

procedure는 lookup, list, aggregate, extremum 중 하나만.
행이 많다고 list가 아니다. 답이 이름·코드·시설 나열인지, 측정값인지로 고른다.
- lookup: 특정 대상의 측정값을 가져온다. 한 시점이든 기간 동안의 변화·추이·추세든 lookup이다.
- list: 이름·코드·시설 등 대상이 무엇인지 나열한다. 측정 숫자가 답이 아니다.
- aggregate: 합·평균·건수 등 집계
- extremum: 가장 높/낮/많/적
기간이 없다고 latest로 바꾸지 마라.

역할(schema_roles)은 연결 구조만. 물리명을 search_terms에 넣지 마라.
metric은 측정 항목만 쓴다. '평균 탁도'가 아니라 '탁도'다. 평균·합계·건수는 procedure/aggregation이다.
집계면 measurement.aggregation에 AVG|SUM|COUNT|MIN|MAX 중 하나를 넣는다.
정의 문장·절·설명('하는 시설', '해당하는 권역')을 넣지 마라.
target은 범위만. primary_outputs 축 이름을 target에 반복하지 마라.
primary_outputs는 답의 축 이름만. 목록·현황 같은 절차 단어를 붙이지 마라.
측정 항목이 답의 축이어도 metric과 schema_roles(측정항목)에서 빼지 마라.
search_terms는 역할의 짧은 별칭만.

출력은 아래 구조의 단일 JSON 객체만 반환하라.

{
  "goal": "한 줄 확보 목표",
  "procedure": "lookup|list|aggregate|extremum",
  "procedure_why": "절차를 고른 이유",
  "metric": "확보할 측정 표현. 없으면 빈 문자열",
  "target": "범위 대상. 없으면 빈 문자열",
  "period": "기간 원문. 없으면 빈 문자열",
  "schema_roles": [
    {
      "role": "확보 역할",
      "necessity": "required|optional",
      "cardinality": "one|many",
      "search_terms": ["짧은 별칭"]
    }
  ],
  "primary_outputs": ["답에 나와야 하는 축"],
  "answer_must_include": ["답이 반드시 포함해야 하는 표현"],
  "meaning_status": "complete|partial"
}
"""


def _user_payload(question: str) -> str:
    return question.strip()


def _ingest_analysis_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Map leftover LLM keys onto the serving analysis fields."""

    roles = payload.pop("meaning_roles", None)
    if roles and not payload.get("schema_roles"):
        payload["schema_roles"] = roles
    intent = payload.pop("intent", None)
    if intent and not payload.get("goal"):
        payload["goal"] = intent
    return payload


def _unique(values: list[str], *, limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _blank_physical(text: str) -> tuple[str, bool]:
    raw = str(text or "").strip()
    if looks_physical_name(raw):
        return "", True
    return raw, False


def _has_meaning_slots(analysis: QueryAnalysis) -> bool:
    return any(
        [
            analysis.goal,
            analysis.procedure,
            analysis.metric,
            analysis.target,
            analysis.period,
            analysis.primary_outputs,
            analysis.answer_must_include,
            analysis.schema_roles,
        ]
    )


def _correct_list_for_measured_series(
    analysis: QueryAnalysis,
    question: str = "",
) -> None:
    """변화·추이는 대상 목록이 아니다. 측정값이 있으면 list를 lookup으로 되돌린다."""

    if str(analysis.procedure or "").strip() != "list":
        return
    metric = str(analysis.metric or analysis.measurement.metric or "").strip()
    if not metric:
        return
    blob = " ".join(
        [
            question,
            str(analysis.goal or ""),
            str(analysis.procedure_why or ""),
        ]
    )
    if _asks_series(blob):
        analysis.procedure = "lookup"


def _dual_write(analysis: QueryAnalysis, question: str = "") -> QueryAnalysis:
    if question:
        analysis.query = question
    raw_metric = str(analysis.metric or analysis.measurement.metric or "")
    if analysis.metric:
        analysis.measurement.metric = analysis.metric
    peeled = measure_item_surface(raw_metric)
    if peeled:
        analysis.metric = peeled
        analysis.measurement.metric = peeled
    _correct_list_for_measured_series(analysis, question)
    procedure = str(analysis.procedure or "").strip()
    if procedure in {"aggregate", "extremum"}:
        if procedure == "extremum":
            asked = extremum_function_from_text(
                " ".join(
                    [
                        str(analysis.goal or ""),
                        str(analysis.procedure_why or ""),
                        raw_metric,
                    ]
                )
            )
            if asked:
                analysis.measurement.aggregation = asked
            elif not analysis.measurement.aggregation:
                analysis.measurement.aggregation = "MAX"
        if "평균" in raw_metric and not analysis.measurement.aggregation:
            analysis.measurement.aggregation = "AVG"
    if procedure in {"list", "lookup", ""}:
        analysis.measurement.aggregation = None
    axis = list(analysis.primary_outputs or [])
    target = str(analysis.target or "").strip()
    kept_filters: list[FilterRequirement] = []
    list_axis = procedure == "list"
    for requirement in analysis.filter_requirements:
        value = str(requirement.value_text or requirement.meaning or "").strip()
        if list_axis and (
            is_answer_axis_text(value, axis)
            or is_answer_axis_text(requirement.meaning, axis)
        ):
            continue
        if target and (
            value == target or requirement.meaning.strip() == target
        ):
            continue
        kept_filters.append(requirement)
    if target and not is_answer_axis_text(target, axis):
        kept_filters.append(
            FilterRequirement(
                meaning="범위 대상",
                required=True,
                operator_hint="EQ",
                value_text=target,
            )
        )
    analysis.filter_requirements = kept_filters
    return analysis


def _sanitize(analysis: QueryAnalysis, question: str = "") -> QueryAnalysis:
    dropped_physical = False
    analysis.goal, hit = _blank_physical(analysis.goal)
    dropped_physical = dropped_physical or hit
    analysis.procedure_why, hit = _blank_physical(analysis.procedure_why)
    dropped_physical = dropped_physical or hit
    analysis.metric, hit = _blank_physical(analysis.metric)
    dropped_physical = dropped_physical or hit
    analysis.target, hit = _blank_physical(analysis.target)
    dropped_physical = dropped_physical or hit
    analysis.period = str(analysis.period or "").strip()[:200]
    analysis.goal = analysis.goal.strip()[:500]
    analysis.procedure_why = analysis.procedure_why.strip()[:500]
    analysis.metric = analysis.metric.strip()[:200]
    analysis.target = analysis.target.strip()[:200]
    analysis.primary_outputs = _unique(
        [
            blank
            for item in analysis.primary_outputs
            for blank, phys in [_blank_physical(item)]
            if not phys and blank
        ],
        limit=12,
    )
    analysis.answer_must_include = _unique(
        [
            blank
            for item in analysis.answer_must_include
            for blank, phys in [_blank_physical(item)]
            if not phys and blank
        ],
        limit=12,
    )
    if any(looks_physical_name(item) for item in analysis.primary_outputs):
        dropped_physical = True
    procedure = str(analysis.procedure or "").strip().casefold()
    if procedure and procedure not in ALLOWED_PROCEDURES:
        analysis.procedure = ""
    else:
        analysis.procedure = procedure

    meaning_status = str(analysis.meaning_status or "").strip().casefold()
    if meaning_status not in {"complete", "partial", "failed"}:
        meaning_status = ""
    if meaning_status == "failed":
        analysis.meaning_status = "failed"
    elif dropped_physical or analysis.procedure == "":
        analysis.meaning_status = "partial" if _has_meaning_slots(analysis) else "failed"
    elif meaning_status:
        analysis.meaning_status = meaning_status
    elif _has_meaning_slots(analysis):
        required_empty = not analysis.primary_outputs
        analysis.meaning_status = "partial" if required_empty else "complete"
    else:
        analysis.meaning_status = "partial"

    analysis.goal = analysis.goal.strip()[:500]
    if str(analysis.measurement.aggregation or "").strip().lower() in {
        "",
        "null",
        "none",
        "없음",
    }:
        analysis.measurement.aggregation = None

    roles: list[SchemaRoleRequirement] = []
    role_names: set[str] = set()
    source_roles = analysis.schema_roles or []
    for role in source_roles[:10]:
        role.role, phys = _blank_physical(role.role)
        if phys:
            dropped_physical = True
            continue
        role.role = role.role.strip()[:200]
        role.search_terms = _unique(
            [
                blank
                for item in role.search_terms
                for blank, term_phys in [_blank_physical(item)]
                if not term_phys and blank
            ],
            limit=8,
        )
        key = role.role.lower()
        if role.role and key not in role_names:
            role_names.add(key)
            roles.append(role)
    analysis.schema_roles = roles
    if dropped_physical and analysis.meaning_status == "complete":
        analysis.meaning_status = "partial"
    if roles and not any(role.necessity == "required" for role in roles):
        roles[0].necessity = "required"

    column_only_roles = {
        role.role
        for role in analysis.schema_roles
        if (
            "메시지" in role.role
            or (
                role.role.endswith(("이름", "명칭", "시각", "일시"))
                and not any(
                    marker in role.role
                    for marker in ("마스터", "이력", "로그", "상태", "데이터")
                )
            )
        )
    }
    if column_only_roles:
        analysis.schema_roles = [
            role
            for role in analysis.schema_roles
            if role.role not in column_only_roles
        ]

    filters: list[FilterRequirement] = []
    for requirement in analysis.filter_requirements[:20]:
        requirement.meaning = requirement.meaning.strip()[:300]
        if requirement.value_text is not None:
            requirement.value_text = requirement.value_text.strip()[:300] or None
            if str(requirement.value_text or "").lower() in {
                "null",
                "none",
                "없음",
            }:
                requirement.value_text = None
        if requirement.meaning:
            filters.append(requirement)
    analysis.filter_requirements = filters
    analysis.status = "complete"
    return _dual_write(analysis, question)


def degraded_analysis(reason: str) -> QueryAnalysis:
    return QueryAnalysis(
        status="degraded",
        reason=reason[:500],
        meaning_status="failed",
    )


class QueryAnalyzer:
    async def analyze(
        self,
        question: str,
        timeout_s: float | None = None,
    ) -> QueryAnalysis:
        runtime = get_runtime()
        if not runtime.decision.hyde_enabled:
            return degraded_analysis("HyDE 의미 분해가 비활성화되어 있습니다.")
        if not runtime.embedding.api_key:
            return degraded_analysis("HyDE 의미 분해용 API key가 없습니다.")
        configured = float(
            getattr(runtime.decision, "analysis_timeout_seconds", 15) or 15
        )
        timeout = configured
        if timeout_s is not None:
            try:
                timeout = max(0.1, min(configured, float(timeout_s)))
            except (TypeError, ValueError):
                timeout = configured
        if timeout < 1:
            return degraded_analysis("파이프라인 시간 부족")
        logger.warning("meaning analyze start timeout=%s", timeout)

        client = AsyncOpenAI(
            api_key=runtime.embedding.api_key,
            base_url=(
                runtime.decision.analysis_base_url
                or runtime.embedding.base_url
                or None
            ),
            timeout=timeout,
            max_retries=0,
        )
        create_kwargs: dict[str, Any] = {
            "model": runtime.decision.hyde_model,
            "messages": [
                {"role": "system", "content": QUERY_ANALYSIS_PROMPT},
                {"role": "user", "content": _user_payload(question)},
            ],
            "max_completion_tokens": 1200,
            "response_format": {"type": "json_object"},
        }
        if getattr(runtime.decision, "analysis_reasoning_effort", False) is True:
            create_kwargs["reasoning_effort"] = "low"
        try:
            async with asyncio.timeout(timeout):
                response = await client.chat.completions.create(**create_kwargs)
            content = response.choices[0].message.content or ""
            payload: Any = json.loads(content)
            if not isinstance(payload, dict):
                return degraded_analysis("HyDE 응답이 JSON 객체가 아닙니다.")
            payload["status"] = "complete"
            analysis = QueryAnalysis.model_validate(_ingest_analysis_payload(payload))
            logger.warning(
                "meaning analyze ok procedure=%s meaning=%s",
                analysis.procedure,
                analysis.meaning_status,
            )
            return _sanitize(analysis, question)
        except TimeoutError:
            logger.warning("meaning analyze timeout")
            return degraded_analysis("HyDE 의미 분해 실패: timeout")
        except Exception as exc:
            logger.warning("meaning analyze fail %s", type(exc).__name__)
            return degraded_analysis(f"HyDE 의미 분해 실패: {exc}")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                maybe = close()
                if asyncio.iscoroutine(maybe):
                    await maybe


def analysis_embedding_text(analysis: QueryAnalysis) -> str:
    parts = [
        analysis.goal,
        analysis.metric or (analysis.measurement.metric or ""),
        analysis.measurement.aggregation or "",
        analysis.target,
        analysis.period,
        *analysis.primary_outputs,
        *(role.role for role in analysis.schema_roles),
        *(requirement.meaning for requirement in analysis.filter_requirements),
    ]
    return "\n".join(value.strip() for value in parts if value and value.strip())


def role_embedding_text(
    analysis: QueryAnalysis,
    role: SchemaRoleRequirement,
) -> str:
    parts = [
        role.role,
        role.role,
        *role.search_terms,
    ]
    text = f"{role.role} {' '.join(role.search_terms)}".casefold()
    fact_seed = "마스터" not in text and (
        "팩트" in text or "시계열" in text
    )
    if not fact_seed:
        parts.extend(
            [
                analysis.goal,
                analysis.metric or (analysis.measurement.metric or ""),
            ]
        )
    return "\n".join(value.strip() for value in parts if value and value.strip())


_analyzer: QueryAnalyzer | None = None


def get_query_analyzer() -> QueryAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = QueryAnalyzer()
    return _analyzer


def set_query_analyzer(analyzer: QueryAnalyzer | None) -> None:
    global _analyzer
    _analyzer = analyzer
