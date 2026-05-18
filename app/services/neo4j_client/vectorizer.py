"""
테이블 벡터라이저 — K-AIR robo-data-text2sql/app/core/text2sql_table_vectorizer.py 기반.
기존 Neo4j Table.text_to_sql_embedding_text를 읽어 OpenAI 임베딩 생성 후 text_to_sql_vector에 저장.
벡터 인덱스도 함께 생성.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Tuple

from neo4j import AsyncGraphDatabase

from .config import settings
from .embedding import get_embedding_client


async def _fetch_tables_missing_vector(session) -> List[Dict[str, Any]]:
    """text_to_sql_embedding_text는 있지만 text_to_sql_vector가 없는 테이블 조회"""
    cypher = """
    MATCH (t:Table)
    WHERE t.text_to_sql_embedding_text IS NOT NULL
      AND t.text_to_sql_embedding_text <> ''
      AND (t.text_to_sql_vector IS NULL OR size(coalesce(t.text_to_sql_vector, [])) = 0)
    RETURN
      elementId(t) AS tid,
      COALESCE(t.schema,'') AS schema,
      COALESCE(t.name,'') AS name,
      substring(t.text_to_sql_embedding_text, 0, 8000) AS embedding_text
    ORDER BY schema ASC, name ASC
    """
    res = await session.run(cypher)
    return [dict(r) async for r in res]


async def _write_vectors(session, *, items: List[Dict[str, Any]]) -> None:
    cypher = """
    UNWIND $items AS item
    MATCH (t) WHERE elementId(t) = item.tid
    SET t.text_to_sql_vector = item.vector,
        t.text_to_sql_updated_at = datetime()
    """
    res = await session.run(cypher, items=items)
    await res.consume()


async def _ensure_vector_index(session) -> None:
    """벡터 인덱스 생성 (IF NOT EXISTS)"""
    cypher = f"""
    CREATE VECTOR INDEX text_to_sql_table_vec_index IF NOT EXISTS
    FOR (t:Table) ON (t.text_to_sql_vector)
    OPTIONS {{
        indexConfig: {{
            `vector.dimensions`: {settings.embedding_dimension},
            `vector.similarity_function`: 'cosine'
        }}
    }}
    """
    try:
        res = await session.run(cypher)
        await res.consume()
        print("[OK] Vector index 'text_to_sql_table_vec_index' ensured.")
    except Exception as exc:
        if "already exists" in str(exc).lower():
            print("[OK] Vector index already exists.")
        else:
            print(f"[WARN] Vector index creation: {exc}")


async def run_vectorizer() -> None:
    """
    메인 벡터라이징 함수.
    1. Neo4j에서 embedding_text 있지만 vector 없는 테이블 조회
    2. OpenAI로 임베딩 생성 (배치)
    3. Neo4j에 vector 저장
    4. 벡터 인덱스 생성
    """
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )

    async with driver.session() as session:
        print("=== Fetching tables missing vectors ===")
        tables = await _fetch_tables_missing_vector(session)
        print(f"  Found {len(tables)} tables to vectorize")

        if not tables:
            print("  All tables already have vectors. Creating index if needed...")
            await _ensure_vector_index(session)
            await driver.close()
            return

        embedder = get_embedding_client()
        batch_size = 100
        total_written = 0
        started = time.perf_counter()

        for i in range(0, len(tables), batch_size):
            batch = tables[i: i + batch_size]
            texts = [t["embedding_text"] for t in batch]
            print(f"  Embedding batch {i // batch_size + 1} ({len(texts)} texts)...")

            vectors = await embedder.embed_batch(texts)

            payload = []
            for t, vec in zip(batch, vectors):
                if vec:
                    payload.append({"tid": t["tid"], "vector": list(vec)})

            if payload:
                await _write_vectors(session, items=payload)
                total_written += len(payload)
                print(f"    Written {len(payload)} vectors (total: {total_written})")

        elapsed_s = time.perf_counter() - started
        print(f"\n=== Vectorization complete: {total_written} tables in {elapsed_s:.1f}s ===")

        await _ensure_vector_index(session)

    await driver.close()


def main():
    """CLI 진입점"""
    asyncio.run(run_vectorizer())


if __name__ == "__main__":
    main()
