"""
K-AIR 방식 메타데이터 API.
파이프라인: HyDE → 벡터 검색 → 테이블 함축 → db_probe(값 해소) → 메타데이터 반환.

K-AIR robo-data-text2sql 파이프라인을 독립 API로 재구성.
"""
from __future__ import annotations

import json
import os
import time
import traceback
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from neo4j import AsyncGraphDatabase

from .config import settings
from .db_probe import batch_db_probe, close_pg_pool, get_pg_pool
from .embedding import get_embedding_client
from .hyde import build_hyde_embedding_text, get_hyde_generator
from .models import (
    ColumnCandidate,
    ResolveQuestionRequest,
    ResolveQuestionResponse,
)
from .vector_search import (
    fetch_anchor_columns,
    fetch_fk_relationships,
    fetch_table_schemas,
    search_tables_by_vector,
)

# ---------------------------------------------------------------------------
# Neo4j driver (앱 수명주기)
# ---------------------------------------------------------------------------

_neo4j_driver = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _neo4j_driver
    _neo4j_driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    print(f"[OK] Neo4j connected: {settings.neo4j_uri}")

    pg_pool = await get_pg_pool()
    if pg_pool:
        print(f"[OK] PostgreSQL connected: {settings.pg_host}:{settings.pg_port}/{settings.pg_database}")
    else:
        print("[WARN] PostgreSQL not available — db_probe will be skipped")

    yield

    await close_pg_pool()
    if _neo4j_driver:
        await _neo4j_driver.close()


# ---------------------------------------------------------------------------
# FastAPI 앱
# ---------------------------------------------------------------------------

app = FastAPI(
    title="K-AIR Style Metadata Resolver API",
    description="벡터 검색 → 테이블 함축 → db_probe 방식의 메타데이터 해소 API",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 헬스체크
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    neo4j_ok = False
    pg_ok = False
    try:
        async with _neo4j_driver.session() as s:
            r = await s.run("RETURN 1")
            await r.consume()
            neo4j_ok = True
    except Exception:
        pass
    try:
        pool = await get_pg_pool()
        if pool:
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
                pg_ok = True
    except Exception:
        pass
    return {"neo4j": neo4j_ok, "postgresql": pg_ok}


# ---------------------------------------------------------------------------
# 유틸: HyDE 결과에서 키워드 추출
# ---------------------------------------------------------------------------

def _extract_keywords_from_hyde(hyde_out, question: str) -> List[str]:
    """HyDE 구조체 + 원본 질문에서 db_probe용 키워드 추출"""
    keywords: list[str] = []
    seen: set[str] = set()

    def _add(items):
        for x in items or []:
            s = str(x or "").strip()
            if s and s.lower() not in seen and len(s) >= 2:
                seen.add(s.lower())
                keywords.append(s)

    if hyde_out:
        _add(hyde_out.entities.include)
        _add(hyde_out.search_keywords.tables)
        _add(hyde_out.search_keywords.columns)
        _add(hyde_out.join_filter_hints.filter_column_meanings)

    # 원본 질문에서 2글자 이상 단어 추출 (조사 제거 간이 처리)
    import re
    tokens = re.findall(r"[가-힣a-zA-Z0-9_]{2,}", question)
    stopwords = {"알려줘", "보여줘", "검색", "조회", "해줘", "어떻게", "얼마나", "무엇", "뭐야"}
    for t in tokens:
        if t.lower() not in seen and t not in stopwords:
            seen.add(t.lower())
            keywords.append(t)

    return keywords[:20]


def _select_probe_columns(
    table_schemas: List[Dict[str, Any]],
    keywords: List[str],
) -> List[ColumnCandidate]:
    """db_probe 대상 컬럼 선정: 문자형 컬럼 중 키워드와 연관 가능성 높은 것"""
    text_types = {"varchar", "text", "char", "character varying", "nvarchar", "bpchar"}
    candidates: list[ColumnCandidate] = []

    for tbl in table_schemas:
        schema = str(tbl.get("schema") or "")
        name = str(tbl.get("name") or "")
        for col in tbl.get("columns") or []:
            col_name = str(col.get("name") or "")
            dtype = str(col.get("dtype") or "").lower().strip()
            desc = str(col.get("description") or "")
            if not col_name:
                continue
            base_type = dtype.split("(")[0].strip()
            if base_type not in text_types:
                continue
            candidates.append(
                ColumnCandidate(
                    table_schema=schema,
                    table_name=name,
                    name=col_name,
                    dtype=dtype,
                    description=desc,
                )
            )

    return candidates[:100]


# ---------------------------------------------------------------------------
# 메인 엔드포인트: POST /rwis/resolve-question
# ---------------------------------------------------------------------------

@app.post("/rwis/resolve-question", response_model=ResolveQuestionResponse)
async def resolve_question(req: ResolveQuestionRequest):
    """
    K-AIR 방식 파이프라인:
    1. HyDE: 질문 → 구조화 힌트 → 임베딩 텍스트
    2. Embed: HyDE 텍스트 + 원본 질문 → 벡터
    3. Vector Search: Neo4j text_to_sql_vector 검색
    4. Schema Fetch: 선택된 테이블의 전체 스키마 조회
    5. db_probe: PostgreSQL에서 실제값 존재 여부 확인 (선택)
    6. Return: 메타데이터 반환
    """
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")

    pipeline_start = time.perf_counter()
    debug: Dict[str, Any] = {"pipeline_steps": []}

    def _step(name: str, data: Any = None) -> None:
        elapsed = (time.perf_counter() - pipeline_start) * 1000
        debug["pipeline_steps"].append({
            "step": name,
            "elapsed_ms": round(elapsed, 1),
            **({"data": data} if data else {}),
        })

    schema_filter = [s.strip() for s in (req.schema_filter or "").split(",") if s.strip()] or None

    # -----------------------------------------------------------------------
    # Step 1: HyDE — 질문 → 구조화 힌트
    # -----------------------------------------------------------------------
    hyde_gen = get_hyde_generator()
    hyde_out, hyde_status = await hyde_gen.generate(question=question)
    hyde_dict = hyde_out.model_dump() if hyde_out else None

    hyde_embed_text = build_hyde_embedding_text(hyde_out) if hyde_out else ""
    _step("hyde", {"status": hyde_status, "embed_text_len": len(hyde_embed_text)})

    # -----------------------------------------------------------------------
    # Step 2: Embed — 임베딩 벡터 생성 (HyDE 텍스트 + 원본 질문)
    # -----------------------------------------------------------------------
    embedder = get_embedding_client()

    texts_to_embed = []
    if hyde_embed_text:
        texts_to_embed.append(hyde_embed_text)
    texts_to_embed.append(question)

    embeddings = await embedder.embed_batch(texts_to_embed)

    hyde_embedding = embeddings[0] if hyde_embed_text else None
    question_embedding = embeddings[-1]
    _step("embed", {"vectors_generated": len(embeddings)})

    # -----------------------------------------------------------------------
    # Step 3: Vector Search — Neo4j 테이블 벡터 검색 (다축)
    # -----------------------------------------------------------------------
    async with _neo4j_driver.session() as neo4j_session:
        # HyDE 벡터로 검색 (주축)
        tables_hyde: list = []
        mode_hyde = ""
        if hyde_embedding:
            tables_hyde, mode_hyde = await search_tables_by_vector(
                neo4j_session,
                embedding=hyde_embedding,
                k=req.top_k,
                schema_filter=schema_filter,
            )

        # 원본 질문 벡터로 검색 (보조축)
        tables_question, mode_question = await search_tables_by_vector(
            neo4j_session,
            embedding=question_embedding,
            k=req.top_k,
            schema_filter=schema_filter,
        )

        # 두 축 병합 + 중복 제거 + 점수 합산
        merged = _merge_table_candidates(tables_hyde, tables_question, top_k=req.top_k)
        _step("vector_search", {
            "hyde_mode": mode_hyde,
            "question_mode": mode_question,
            "hyde_results": len(tables_hyde),
            "question_results": len(tables_question),
            "merged": len(merged),
        })

        # -----------------------------------------------------------------------
        # Step 4: Schema Fetch — 선택된 테이블의 전체 스키마 (컬럼, FK 등)
        # -----------------------------------------------------------------------
        table_schemas = await fetch_table_schemas(neo4j_session, tables=merged)

        table_fqns = [f"{t.schema}.{t.name}" for t in merged if t.name]
        fk_rels = await fetch_fk_relationships(neo4j_session, table_fqns=table_fqns)

        _step("schema_fetch", {
            "tables": len(table_schemas),
            "fk_relationships": len(fk_rels),
        })

    # -----------------------------------------------------------------------
    # Step 5: db_probe — PostgreSQL 실제값 존재 확인 (선택)
    # -----------------------------------------------------------------------
    probe_results: Optional[Dict[str, Any]] = None
    if req.enable_db_probe:
        keywords = _extract_keywords_from_hyde(hyde_out, question)
        probe_columns = _select_probe_columns(table_schemas, keywords)

        if keywords and probe_columns:
            raw_probe = await batch_db_probe(keywords=keywords, columns=probe_columns)
            if raw_probe:
                probe_results = raw_probe
                _step("db_probe", {
                    "keywords": keywords,
                    "probe_columns": len(probe_columns),
                    "matches": sum(len(v) for v in raw_probe.values()),
                })
            else:
                _step("db_probe", {"status": "no_matches_or_pg_unavailable"})
        else:
            _step("db_probe", {"status": "skipped_no_keywords_or_columns"})
    else:
        _step("db_probe", {"status": "disabled"})

    # -----------------------------------------------------------------------
    # Step 6: 응답 조립
    # -----------------------------------------------------------------------
    total_ms = (time.perf_counter() - pipeline_start) * 1000
    debug["total_ms"] = round(total_ms, 1)
    debug["tables_found"] = len(merged)

    return ResolveQuestionResponse(
        question=question,
        hyde=hyde_dict,
        selected_tables=[
            {
                "schema": t.schema,
                "name": t.name,
                "description": t.description,
                "analyzed_description": t.analyzed_description,
                "score": round(t.score, 4),
            }
            for t in merged
        ],
        table_schemas=table_schemas,
        fk_relationships=fk_rels,
        db_probe_results=probe_results,
        debug=debug,
    )


# ---------------------------------------------------------------------------
# 유틸: 다축 검색 결과 병합
# ---------------------------------------------------------------------------

def _merge_table_candidates(
    hyde_tables: list,
    question_tables: list,
    *,
    top_k: int = 10,
    hyde_weight: float = 0.6,
    question_weight: float = 0.4,
):
    """K-AIR 스타일 다축 검색 결과 병합 (가중 점수 합산)"""
    from .models import TableCandidate

    scores: Dict[str, float] = {}
    by_fqn: Dict[str, TableCandidate] = {}

    for t in hyde_tables:
        fqn = t.fqn.lower()
        scores[fqn] = scores.get(fqn, 0.0) + t.score * hyde_weight
        if fqn not in by_fqn:
            by_fqn[fqn] = t

    for t in question_tables:
        fqn = t.fqn.lower()
        scores[fqn] = scores.get(fqn, 0.0) + t.score * question_weight
        if fqn not in by_fqn:
            by_fqn[fqn] = t

    sorted_fqns = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)[:top_k]

    result = []
    for fqn in sorted_fqns:
        t = by_fqn[fqn]
        result.append(
            TableCandidate(
                schema=t.schema,
                name=t.name,
                description=t.description,
                analyzed_description=t.analyzed_description,
                score=scores[fqn],
            )
        )
    return result


# ---------------------------------------------------------------------------
# 추가 엔드포인트: GET /tables — 전체 테이블 목록
# ---------------------------------------------------------------------------

@app.get("/tables")
async def list_tables(schema: Optional[str] = None, db_exists_only: bool = True):
    cypher = """
    MATCH (t:Table)
    WHERE ($schema IS NULL OR toLower(t.schema) = toLower($schema))
      AND ($db_exists = false OR t.text_to_sql_db_exists = true)
    RETURN t.schema AS schema, t.name AS name, t.description AS description,
           t.text_to_sql_table_type AS table_type,
           t.text_to_sql_db_exists AS db_exists
    ORDER BY schema, name
    """
    async with _neo4j_driver.session() as s:
        res = await s.run(cypher, schema=schema, db_exists=db_exists_only)
        return await res.data()


# ---------------------------------------------------------------------------
# 추가 엔드포인트: GET /vector-status — 벡터라이징 현황
# ---------------------------------------------------------------------------

@app.get("/vector-status")
async def vector_status():
    async with _neo4j_driver.session() as s:
        r1 = await s.run("MATCH (t:Table) RETURN count(t) AS cnt")
        total = (await r1.single())["cnt"]

        r2 = await s.run(
            "MATCH (t:Table) WHERE t.text_to_sql_vector IS NOT NULL "
            "AND size(t.text_to_sql_vector) > 0 RETURN count(t) AS cnt"
        )
        vectorized = (await r2.single())["cnt"]

        r3 = await s.run("SHOW INDEXES YIELD name, type WHERE type = 'VECTOR' RETURN name")
        indexes = [dict(r)["name"] async for r in r3]

    return {
        "total_tables": total,
        "vectorized_tables": vectorized,
        "coverage_pct": round(vectorized / total * 100, 1) if total else 0,
        "vector_indexes": indexes,
    }


# ---------------------------------------------------------------------------
# 추가 엔드포인트: POST /vectorize — 벡터라이징 실행
# ---------------------------------------------------------------------------

@app.post("/vectorize")
async def trigger_vectorize():
    from .vectorizer import run_vectorizer
    try:
        await run_vectorizer()
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# 서버 구동
# ---------------------------------------------------------------------------

def main():
    import uvicorn
    print(f"Starting API server on {settings.host}:{settings.port}")
    uvicorn.run(
        "neo4j_client.api:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
