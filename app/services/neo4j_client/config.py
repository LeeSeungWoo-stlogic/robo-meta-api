"""
neo4j_client 설정.
환경변수 또는 기본값으로 구성.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # Neo4j
    neo4j_uri: str = field(default_factory=lambda: os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687"))
    neo4j_user: str = field(default_factory=lambda: os.getenv("NEO4J_USER", "neo4j"))
    neo4j_password: str = field(default_factory=lambda: os.getenv("NEO4J_PASSWORD", ""))

    # OpenAI (GenOS LLMOps: chat·embedding serving_id 별 base_url 분리)
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL", ""))
    openai_embedding_base_url: str = field(
        default_factory=lambda: os.getenv("OPENAI_EMBEDDING_BASE_URL", "")
    )
    embedding_model: str = field(
        default_factory=lambda: os.getenv(
            "OPENAI_EMBEDDING_MODEL",
            os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        )
    )
    embedding_dimension: int = field(
        default_factory=lambda: int(
            os.getenv("OPENAI_EMBEDDING_DIM", os.getenv("EMBEDDING_DIMENSION", "1536"))
        )
    )
    llm_model: str = field(
        default_factory=lambda: os.getenv(
            "OPENAI_LLM_MODEL", os.getenv("LLM_MODEL", "gpt-4o-mini")
        )
    )

    # PostgreSQL (db_probe용, 선택사항)
    pg_host: str = field(default_factory=lambda: os.getenv("PG_HOST", "127.0.0.1"))
    pg_port: int = field(default_factory=lambda: int(os.getenv("PG_PORT", "5432")))
    pg_database: str = field(default_factory=lambda: os.getenv("PG_DATABASE", "rwis"))
    pg_user: str = field(
        default_factory=lambda: os.getenv("PG_USER") or os.getenv("SOURCE_PG_USER", "postgres")
    )
    pg_password: str = field(
        default_factory=lambda: os.getenv("PG_PASSWORD")
        or os.getenv("SOURCE_PG_PASS")
        or os.getenv("SOURCE_PG_PASSWORD", "")
    )
    pg_schemas: str = field(default_factory=lambda: os.getenv("PG_SCHEMAS", "RWIS"))

    # API
    host: str = field(default_factory=lambda: os.getenv("API_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("API_PORT", "8095")))

    # 벡터 검색 파라미터
    vector_search_k: int = 15
    column_search_per_table_k: int = 20
    db_probe_timeout_s: float = 3.0
    db_probe_value_limit: int = 5


settings = Settings()
