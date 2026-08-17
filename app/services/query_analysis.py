"""Backend-independent natural-language requirement analysis for v1 decisions."""
from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from ..runtime_config import get_runtime
from ..schemas import (
    FilterRequirement,
    JoinRequirement,
    QueryAnalysis,
    SchemaRoleRequirement,
)


QUERY_ANALYSIS_PROMPT = """\
당신은 Text-to-SQL의 메타데이터 선택을 위한 요구사항 분석기입니다.
사용자 질문을 실제 DB 물리 이름을 추측하지 않고 업무 의미와 역할로만 분해하세요.

규칙:
- 실제 테이블명과 컬럼명을 만들지 마세요.
- schema_roles의 role은 "사업장" 같은 엔터티 명사만 쓰지 말고
  "사업장 명칭 마스터", "사무소 연결 상태"처럼 데이터의 업무 역할을 쓰세요.
- 오류 메시지, 측정값, 이름, 코드, 시각처럼 기존 역할 테이블의 컬럼으로
  해결되는 항목은 별도 schema_role로 만들지 말고 measurement,
  filter_requirements, search_keywords.columns에만 넣으세요.
- schema_role 하나는 서로 다른 물리 테이블 책임 후보 하나를 뜻합니다.
- 각 역할의 search_terms에는 그 역할을 다른 역할과 구별할 테이블·컬럼 의미를
  2~6개 넣으세요. 측정값·상태·명칭 중 어느 역할이 보유해야 하는지 구분하세요.
- 원문에 명시되어 반드시 필요한 역할만 necessity="required"로 표시하세요.
- 보조적으로 유용하지만 없어도 답할 수 있는 역할은 necessity="optional"입니다.
- JOIN은 역할 간 요구사항이며, 복수 키 의미를 key_meanings 배열로 보존하세요.
- 필터 연산자는 EQ, NE, IN, BETWEEN, GT, GTE, LT, LTE, LIKE, ILIKE,
  IS_NULL, IS_NOT_NULL 중 하나만 사용하세요.
- 값이 원문에 있으면 value_text에 원문 표현을 보존하세요.
- 사용자 메시지에 스토어 후보(용어·약어·값매핑·논리명)가 있으면 그 후보만
  해석 재료로 쓰세요. 후보에 있는 column_fqn만 인용하고 없는 물리표를 만들지 마세요.
- 접두로 여러 라벨이 있으면 entities_include와 filter value_text에
  질문과 가장 맞는 스토어 라벨 하나를 쓰세요.
- storage_type_hint는 물리 테이블명이 아니라 입도만 쓰세요: month|day|hour|instant|null.
- "4월"·"어제"는 필터 기간입니다. 사용자가 월별/일별/시간별을 말하지 않으면
  기간 길이에 맞는 입도를 힌트로 두세요 (한 달→month, 어제→day).
- 기간 팩트 역할은 "월별 계측 팩트"처럼 입도를 넣고, 실제 테이블명을 search_terms에 넣지 마세요.
- 출력은 아래 구조의 단일 JSON 객체만 반환하세요.

{
  "intent": "한 줄 의도",
  "entities_include": ["질문에 포함된 엔터티"],
  "entities_exclude": [],
  "measurement": {
    "metric": "측정 또는 조회 대상 의미",
    "aggregation": "AVG|SUM|COUNT|MIN|MAX|null",
    "storage_type_hint": null
  },
  "schema_roles": [
    {
      "role": "업무 데이터 역할",
      "necessity": "required|optional",
      "cardinality": "one|many",
      "search_terms": ["역할 전용 테이블·컬럼 의미"]
    }
  ],
  "join_requirements": [
    {
      "from_role": "역할",
      "to_role": "역할",
      "required": true,
      "key_meanings": ["키 의미"]
    }
  ],
  "filter_requirements": [
    {
      "meaning": "필터 의미",
      "required": true,
      "operator_hint": "EQ",
      "value_text": "원문 값 또는 null"
    }
  ],
  "search_keywords": {
    "tables": ["역할 중심 검색어"],
    "columns": ["컬럼 의미 검색어"]
  }
}
"""


def _user_payload(question: str, store_hits: dict[str, Any] | None) -> str:
    text = question.strip()
    if not store_hits:
        return text
    blob = json.dumps(store_hits, ensure_ascii=False, default=str)
    return f"{text}\n\n스토어 후보:\n{blob}"


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


def _sanitize(analysis: QueryAnalysis) -> QueryAnalysis:
    analysis.intent = analysis.intent.strip()[:500]
    analysis.entities_include = _unique(analysis.entities_include, limit=15)
    analysis.entities_exclude = _unique(analysis.entities_exclude, limit=15)
    analysis.search_keywords.tables = _unique(
        analysis.search_keywords.tables, limit=12
    )
    analysis.search_keywords.columns = _unique(
        analysis.search_keywords.columns, limit=12
    )
    if str(analysis.measurement.aggregation or "").strip().lower() in {
        "",
        "null",
        "none",
        "없음",
    }:
        analysis.measurement.aggregation = None
    if str(analysis.measurement.storage_type_hint or "").strip().lower() in {
        "",
        "null",
        "none",
        "없음",
    }:
        analysis.measurement.storage_type_hint = None

    roles: list[SchemaRoleRequirement] = []
    role_names: set[str] = set()
    for role in analysis.schema_roles[:10]:
        role.role = role.role.strip()[:200]
        role.search_terms = _unique(role.search_terms, limit=8)
        key = role.role.lower()
        if role.role and key not in role_names:
            role_names.add(key)
            roles.append(role)
    analysis.schema_roles = roles
    if roles and not any(role.necessity == "required" for role in roles):
        roles[0].necessity = "required"

    joins: list[JoinRequirement] = []
    for join in analysis.join_requirements[:20]:
        join.from_role = join.from_role.strip()[:200]
        join.to_role = join.to_role.strip()[:200]
        join.key_meanings = _unique(join.key_meanings, limit=10)
        if (
            join.from_role
            and join.to_role
            and join.from_role.lower() in role_names
            and join.to_role.lower() in role_names
            and join.from_role.lower() != join.to_role.lower()
        ):
            joins.append(join)
    analysis.join_requirements = joins

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
        roles_by_name = {
            role.role: role for role in analysis.schema_roles
        }
        for column_role in column_only_roles:
            parent_name = next(
                (
                    requirement.from_role
                    if requirement.to_role == column_role
                    else requirement.to_role
                    for requirement in analysis.join_requirements
                    if column_role
                    in {requirement.from_role, requirement.to_role}
                    and (
                        requirement.from_role
                        if requirement.to_role == column_role
                        else requirement.to_role
                    )
                    not in column_only_roles
                ),
                None,
            )
            if parent_name and parent_name in roles_by_name:
                parent = roles_by_name[parent_name]
                child = roles_by_name[column_role]
                parent.search_terms = _unique(
                    [*parent.search_terms, column_role, *child.search_terms],
                    limit=8,
                )
        analysis.schema_roles = [
            role
            for role in analysis.schema_roles
            if role.role not in column_only_roles
        ]
        analysis.join_requirements = [
            requirement
            for requirement in analysis.join_requirements
            if requirement.from_role not in column_only_roles
            and requirement.to_role not in column_only_roles
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
    return analysis


def degraded_analysis(reason: str) -> QueryAnalysis:
    return QueryAnalysis(
        status="degraded",
        reason=reason[:500],
        fallback="question_vector",
    )


class QueryAnalyzer:
    async def analyze(
        self,
        question: str,
        store_hits: dict[str, Any] | None = None,
    ) -> QueryAnalysis:
        runtime = get_runtime()
        if not runtime.decision.hyde_enabled:
            return degraded_analysis("HyDE 의미 분해가 비활성화되어 있습니다.")
        if not runtime.embedding.api_key:
            return degraded_analysis("HyDE 의미 분해용 API key가 없습니다.")

        client = AsyncOpenAI(
            api_key=runtime.embedding.api_key,
            base_url=(
                runtime.decision.analysis_base_url
                or runtime.embedding.base_url
                or None
            ),
        )
        try:
            response = await client.chat.completions.create(
                model=runtime.decision.hyde_model,
                messages=[
                    {"role": "system", "content": QUERY_ANALYSIS_PROMPT},
                    {
                        "role": "user",
                        "content": _user_payload(question, store_hits),
                    },
                ],
                max_completion_tokens=1200,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or ""
            payload: Any = json.loads(content)
            if not isinstance(payload, dict):
                return degraded_analysis("HyDE 응답이 JSON 객체가 아닙니다.")
            payload["status"] = "complete"
            analysis = QueryAnalysis.model_validate(payload)
            if not analysis.intent or not analysis.schema_roles:
                return degraded_analysis("HyDE 응답에 intent 또는 schema_roles가 없습니다.")
            return _sanitize(analysis)
        except Exception as exc:
            return degraded_analysis(f"HyDE 의미 분해 실패: {exc}")


def analysis_embedding_text(analysis: QueryAnalysis) -> str:
    parts = [
        analysis.intent,
        analysis.measurement.metric or "",
        analysis.measurement.aggregation or "",
        analysis.measurement.storage_type_hint or "",
        *analysis.entities_include,
        *analysis.search_keywords.tables,
        *analysis.search_keywords.columns,
        *(role.role for role in analysis.schema_roles),
        *(
            meaning
            for requirement in analysis.join_requirements
            for meaning in requirement.key_meanings
        ),
        *(requirement.meaning for requirement in analysis.filter_requirements),
    ]
    return "\n".join(value.strip() for value in parts if value and value.strip())


def role_embedding_text(
    analysis: QueryAnalysis,
    role: SchemaRoleRequirement,
) -> str:
    related_keys = [
        meaning
        for requirement in analysis.join_requirements
        if role.role in {requirement.from_role, requirement.to_role}
        for meaning in requirement.key_meanings
    ]
    parts = [
        role.role,
        role.role,
        *role.search_terms,
        *related_keys,
    ]
    text = f"{role.role} {' '.join(role.search_terms)}".casefold()
    fact_seed = "마스터" not in text and (
        "팩트" in text or "시계열" in text
    )
    if not fact_seed:
        parts.extend(
            [
                analysis.intent,
                analysis.measurement.metric or "",
                *analysis.search_keywords.tables,
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
