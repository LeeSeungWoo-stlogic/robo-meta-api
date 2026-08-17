"""T2SQL LLM confirm / generate. Injectable for tests."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from ...runtime_config import T2SqlRuntime, get_runtime

ConfirmFn = Callable[[str, dict[str, Any], float], Awaitable[dict[str, Any]]]
GenerateFn = Callable[[str, dict[str, Any], float], Awaitable[str]]


class _Unset:
    pass


_UNSET = _Unset()
_confirm_override: ConfirmFn | None | _Unset = _UNSET
_generate_override: GenerateFn | None | _Unset = _UNSET


def set_t2sql_llm(
    *,
    confirm: ConfirmFn | None = None,
    generate: GenerateFn | None = None,
) -> None:
    global _confirm_override, _generate_override
    _confirm_override = confirm
    _generate_override = generate


def reset_t2sql_llm() -> None:
    global _confirm_override, _generate_override
    _confirm_override = _UNSET
    _generate_override = _UNSET


CONFIRM_PROMPT = """\
당신은 Text-to-SQL 의도 확인기다.
query_plan.filters 중 resolution_status=resolved 인 항목은 이미 확정이다. 그 슬롯을 missing에 넣지 마라.
resolved_entities에 코드가 여러 개여도 query_plan.filters의 value가 SoT다. 코드를 다시 고르거나 창작하지 마라.
질의가 기간을 밝히지 않으면 기간을 missing으로 넣지 마라. 날짜를 창작하지 마라.
probe_rows가 비어 있어도 위 SoT가 있으면 accept=true 다.
query_analysis와 probe만 보고 재판하지 마라.
의도를 다시 쓰지 마라. 출력은 JSON 객체 하나뿐이다.
{"accept": true, "missing": []}
accept=false 이면 missing에 부족한 슬롯 이름을 넣어라.
"""

GENERATE_PROMPT = """\
당신은 읽기 전용 SQL 생성기다.
출력은 SELECT 또는 WITH ... SELECT 한 문장만. 설명·코드펜스 금지. 최신값 보정에는 WITH/CTE를 쓰지 마라.
테이블은 SourceName.Schema.Table 3단 수식과 백틱 인용(`)을 사용하라.
Tibero면 식별자를 인용하라. 쓰기는 금지.
fact 테이블은 WHERE 필터 없이 SELECT 하지 마라.
목록·추이 질의에는 LIMIT를 넣지 마라 (검증용이 아님).
시계열(추이·변화·추세·트렌드)이면 팩트의 측정점 키와 태그 마스터의 명칭·설명을 SELECT에 넣어라. 시각·측정값과 함께 출력하고 측정점 키를 빼지 마라. ORDER BY 측정점 키, 시각. 전일 대비 증감·LAG·차분 컬럼을 창작하지 마라. 원천 측정값만 출력하라.
태그 마스터 SELECT는 측정점 식별과 명칭·설명만. JOIN에 쓰는 상위 코드·주소·경로·산출 컬럼은 SELECT하지 마라.
NOT_LIKE는 column NOT LIKE value 이다. value의 % 패턴을 바꾸지 마라.
time_role이 latest 이면 최신 한 건은 LIMIT 1이 아니라 MAX다. fact 전체를 ORDER BY 한 뒤 LIMIT 1 하지 마라.
Postgres면 Store original_name 대소문자를 유지하고 대문자로 인용하지 마라.
query_plan.required_tables의 table_name이 SQL 식별자다. 없는 표를 창작하지 마라.
query_plan.filters 중 resolution_status=resolved 인 항목은 WHERE SoT다. value의 각 코드는 항상 따옴표 문자 리터럴이다. IN (981) 금지. IN ('981') 이다. 코드를 창작하지 마라. 이미 인라인으로 풀린 필터를 위해 코드 테이블을 JOIN하지 마라.
resolution_status=unresolved 인 필터의 value를 WHERE 리터럴로 쓰지 마라. 코드를 창작하지 마라.
query_plan.join_paths[].conditions는 ON SoT다. path의 condition을 빼지 마라(복합키). 승인되지 않은 JOIN 엣지를 창작하지 마라.
SQL 표는 query_plan.required_tables와 join_paths의 표만 써라.
glossary_routes[].standard_term은 승인 용어집 표준명이다. mention을 그 이름으로 해석하라. SQL 식별자가 아니다. 용어만으로 테이블을 창작하지 마라.
기간은 query_plan.filters의 resolved 기간만 써라. 날짜 컬럼을 창작하지 마라. 오늘·연도 접두 등 날짜 리터럴을 창작하지 마라.
SELECT 목록에 * 나 table.* 나 alias.`*` 를 쓰지 마라. required_columns에 있는 컬럼만 써라.
query_plan.unresolved_requirements에 팩트 표 미선정이 있고 측정·기간이 필요하면 NO_SQL: 팩트 표 미선정 만 출력하라. 차원 표만으로 측정 SELECT를 만들지 마라. 목록 질의는 차원 required_tables로 SELECT를 출력하라. 목록·그룹 대상 차원은 required_columns의 명칭·코드를 SELECT에 넣어라. JOIN 키만 출력하지 마라.
required_columns가 비면 측정 컬럼과 * 를 창작하지 마라.
팩트 표 미선정이 아닐 때 required_tables나 resolved 필터가 있으면 SELECT를 출력하라. unresolved 칸만으로 NO_SQL 하지 마라.
query_plan.time_role이 latest 이고 선택한 fact 표의 required_columns에 측정시각 컬럼이 있으면, 같은 JOIN과 resolved 코드 필터를 유지하고 fact.time_col = (SELECT MAX(fact2.time_col) FROM same_fact fact2 <same JOINs> WHERE <same resolved filters>) 를 더하라. time_role이 extremum 또는 none 이면 이 최신시각 MAX를 넣지 마라.
time_role이 extremum 이면 측정값 극값은 같은 JOIN과 resolved 필터로 ORDER BY 측정컬럼 DESC(가장 높/많) 또는 ASC(가장 낮/적) 한 뒤 LIMIT 1 하라. VAL = (SELECT MAX(VAL) FROM 동일 JOIN 반복) 는 쓰지 마라.
IN (SELECT ...) 는 허용한다. latest의 시각 MAX 안에도 같은 JOIN+WHERE를 반복하라.
time_role이 latest가 아니면 fact 전체를 ORDER BY 한 뒤 LIMIT 1 하는 극값만 허용한다. 더 굵은 입도의 표로 바꾸지 마라.
측정값은 선정된 fact 표의 required_columns만 써라. 컬럼 목록이 비면 숫자 컬럼을 창작하지 마라.
table_notes는 계획에 이미 있는 표의 설명 부록이다. 여기서 표를 다시 고르거나 추가하지 마라.
팩트 표 미선정인 경우를 제외하고, query_plan에 required_tables 또는 resolved 필터가 있는데 SELECT를 비우지 마라. 표와 필터가 모두 없을 때 NO_SQL: 이유 한 줄만 출력하라.
"""


def _client(settings: T2SqlRuntime) -> AsyncOpenAI:
    runtime = get_runtime()
    return AsyncOpenAI(
        api_key=runtime.embedding.api_key or "test",
        base_url=settings.base_url,
        timeout=settings.generate_timeout_seconds,
    )


async def confirm_intent(
    question: str,
    payload: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    if not isinstance(_confirm_override, _Unset):
        if _confirm_override is None:
            raise RuntimeError("t2sql confirm LLM is unset")
        return await _confirm_override(question, payload, timeout_s)
    settings = get_runtime().t2sql
    if settings is None or not settings.configured():
        raise RuntimeError("t2sql is not configured")
    client = _client(settings)
    response = await client.chat.completions.create(
        model=str(settings.model),
        messages=[
            {"role": "system", "content": CONFIRM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"question": question, **payload},
                    ensure_ascii=False,
                ),
            },
        ],
        max_completion_tokens=400,
        response_format={"type": "json_object"},
        timeout=timeout_s,
    )
    content = response.choices[0].message.content or "{}"
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        return {"accept": False, "missing": ["invalid_confirm"]}
    return parsed


async def generate_sql(
    question: str,
    payload: dict[str, Any],
    timeout_s: float,
) -> str:
    if not isinstance(_generate_override, _Unset):
        if _generate_override is None:
            raise RuntimeError("t2sql generate LLM is unset")
        return await _generate_override(question, payload, timeout_s)
    settings = get_runtime().t2sql
    if settings is None or not settings.configured():
        raise RuntimeError("t2sql is not configured")
    client = _client(settings)
    response = await client.chat.completions.create(
        model=str(settings.model),
        messages=[
            {"role": "system", "content": GENERATE_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"question": question, **payload},
                    ensure_ascii=False,
                ),
            },
        ],
        max_completion_tokens=4096,
        timeout=timeout_s,
    )
    return (response.choices[0].message.content or "").strip()
