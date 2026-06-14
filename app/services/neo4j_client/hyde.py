"""
HyDE 스키마 힌트 생성기 — K-AIR robo-data-text2sql/app/react/generators/hyde_schema_generator.py 기반.
langchain 의존성 제거, openai 직접 사용으로 경량화.
"""
from __future__ import annotations

import json
import time
from typing import Any, List, Optional, Tuple

from openai import AsyncOpenAI

from .config import settings
from .models import HydeSchemaOut

# ---------------------------------------------------------------------------
# HyDE 시스템 프롬프트 (K-AIR prompts/hyde_schema_prompt.md 원본)
# ---------------------------------------------------------------------------
HYDE_SYSTEM_PROMPT = """\
당신은 Text-to-SQL을 위한 "HyDE-Schema(가상 스키마 힌트)"를 생성하는 도구입니다.
목표: 사용자의 질문을 DB 스키마 벡터 검색(테이블/컬럼 임베딩 검색)에 유리하도록 구조화된 힌트를 생성합니다.

중요 규칙:
- 실제 DB 스키마(테이블명/컬럼명)를 모르면 절대 만들어내지 마세요. 구체 테이블/컬럼명 추측 금지.
- 대신 "역할(role)"과 "컬럼 의미"를 명확한 키워드로 적으세요.
  예: "태그 마스터", "일/시간 로그", "설명/명칭 컬럼", "측정값 컬럼", "태그ID", "시설/조직 코드"
- 사용자가 말한 엔터티/키워드는 그대로 보존하세요(예: 지명/조직명/제품명/기간/집계어/측정항목 등 사용자가 실제로 언급한 표현).
- 단정 금지. "후보/가능" 표현을 사용하세요.

출력 형식(반드시 지킬 것):
- 출력은 오직 **단일 JSON 객체 1개**만. (마크다운/코드블록/추가 텍스트 금지)
- 키 배열은 3~10개 정도의 짧은 문자열로 작성하세요(빈 문자열 금지).

JSON 스키마(추가 키 금지):
{
  "intent": "한 줄 의도",
  "entities": {
    "include": ["포함키워드1", "포함키워드2"],
    "exclude": ["제외키워드1"]
  },
  "measurement": {
    "aggregation": "AVG|SUM|COUNT|MIN|MAX|기타",
    "metric_meaning": "측정값 의미",
    "storage_type_hint": "수치/문자/캐스팅 필요 여부 등"
  },
  "schema_roles": ["역할1", "역할2"],
  "join_filter_hints": {
    "join_keys": ["조인키 의미 후보1", "조인키 의미 후보2"],
    "filter_column_meanings": ["필터 컬럼 의미 후보1"],
    "needs_time_range": true
  },
  "search_keywords": {
    "tables": ["테이블검색키워드1", "테이블검색키워드2"],
    "columns": ["컬럼검색키워드1", "컬럼검색키워드2"]
  }
}"""


# ---------------------------------------------------------------------------
# JSON 파싱 유틸 (K-AIR 원본)
# ---------------------------------------------------------------------------

def _strip_code_fences(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    if s.startswith("```"):
        lines = s.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            inner = "\n".join(lines[1:-1])
        else:
            inner = "\n".join(lines[1:])
        return inner.strip()
    return s


def _try_parse_hyde_json(text: str) -> Optional[HydeSchemaOut]:
    s = _strip_code_fences(text)
    if not s:
        return None
    obj: Any
    try:
        obj = json.loads(s)
    except Exception:
        left = s.find("{")
        right = s.rfind("}")
        if left >= 0 and right > left:
            try:
                obj = json.loads(s[left: right + 1])
            except Exception:
                return None
        else:
            return None
    if not isinstance(obj, dict):
        return None
    try:
        return HydeSchemaOut.model_validate(obj)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HyDE 구조 정규화 (K-AIR 원본)
# ---------------------------------------------------------------------------

def _sanitize_hyde_structured(out: HydeSchemaOut) -> HydeSchemaOut:
    def _norm_list(xs: list[str], *, limit: int) -> list[str]:
        uniq: list[str] = []
        seen: set[str] = set()
        for x in xs or []:
            s = str(x or "").strip()
            if not s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(s)
            if len(uniq) >= limit:
                break
        return uniq

    out.intent = (out.intent or "").strip()
    out.schema_roles = _norm_list(out.schema_roles, limit=10)
    out.entities.include = _norm_list(out.entities.include, limit=15)
    out.entities.exclude = _norm_list(out.entities.exclude, limit=15)
    out.join_filter_hints.join_keys = _norm_list(out.join_filter_hints.join_keys, limit=10)
    out.join_filter_hints.filter_column_meanings = _norm_list(
        out.join_filter_hints.filter_column_meanings, limit=10
    )
    out.search_keywords.tables = _norm_list(out.search_keywords.tables, limit=10)
    out.search_keywords.columns = _norm_list(out.search_keywords.columns, limit=10)
    out.measurement.aggregation = (out.measurement.aggregation or "").strip()[:40]
    out.measurement.metric_meaning = (out.measurement.metric_meaning or "").strip()[:200]
    out.measurement.storage_type_hint = (out.measurement.storage_type_hint or "").strip()[:200]
    return out


# ---------------------------------------------------------------------------
# 임베딩 텍스트 빌드 (K-AIR 원본)
# ---------------------------------------------------------------------------

def build_hyde_embedding_text(out: HydeSchemaOut, *, max_chars: int = 8000) -> str:
    def _join_list(xs: list[str], *, limit: int) -> str:
        picked: list[str] = []
        seen: set[str] = set()
        for x in xs or []:
            s = str(x or "").strip()
            if not s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            picked.append(s)
            if len(picked) >= int(limit):
                break
        return ", ".join(picked)

    parts: list[str] = []
    kw_tables = _join_list(out.search_keywords.tables, limit=10)
    kw_cols = _join_list(out.search_keywords.columns, limit=10)
    if kw_tables:
        parts.append(kw_tables)
    if kw_cols:
        parts.append(kw_cols)

    ent_incl = _join_list(out.entities.include, limit=15)
    ent_excl = _join_list(out.entities.exclude, limit=15)
    if ent_incl:
        parts.append(ent_incl)
    if ent_excl:
        parts.append(ent_excl)

    intent = (out.intent or "").strip()
    if intent:
        parts.append(intent)

    agg = (out.measurement.aggregation or "").strip()
    meaning = (out.measurement.metric_meaning or "").strip()
    stype = (out.measurement.storage_type_hint or "").strip()
    for v in [agg, meaning, stype]:
        if v:
            parts.append(v)

    if out.schema_roles:
        roles = _join_list(out.schema_roles, limit=10)
        if roles:
            parts.append(roles)

    join_keys = _join_list(out.join_filter_hints.join_keys, limit=10)
    filters = _join_list(out.join_filter_hints.filter_column_meanings, limit=10)
    for v in [join_keys, filters]:
        if v:
            parts.append(v)
    if out.join_filter_hints.needs_time_range is True:
        parts.append("예")

    text = "\n".join(p for p in (s.strip() for s in parts) if p).strip()
    if not text:
        return ""
    return text[:max_chars].rstrip()


# ---------------------------------------------------------------------------
# HyDE 생성기 (K-AIR 원본 로직, langchain → openai 직접 호출로 경량화)
# ---------------------------------------------------------------------------

class HydeSchemaGenerator:
    def __init__(self) -> None:
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)
        self.model = settings.llm_model
        self.system_prompt = HYDE_SYSTEM_PROMPT

    async def generate(
        self, *, question: str
    ) -> Tuple[Optional[HydeSchemaOut], str]:
        q = (question or "").strip()
        if not q:
            return None, "empty_question"

        try:
            started = time.perf_counter()
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": q},
                ],
                temperature=0.0,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            content = (resp.choices[0].message.content or "").strip()
            out = _try_parse_hyde_json(content)
            if out is None:
                return None, "llm_empty"
            out = _sanitize_hyde_structured(out)
            embed_text = build_hyde_embedding_text(out)
            if not embed_text or len(embed_text) < 20:
                return None, "llm_empty"
            return out, f"llm_ok ({elapsed_ms:.0f}ms)"
        except Exception as exc:
            return None, f"llm_error: {exc}"


_generator: HydeSchemaGenerator | None = None


def get_hyde_generator() -> HydeSchemaGenerator:
    global _generator
    if _generator is None:
        _generator = HydeSchemaGenerator()
    return _generator
