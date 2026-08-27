"""robo-meta-api 통합 설정.

K-AIR-meta-api/app/config.py 의 v0.6 RC 정책 키 + Neo4j 베이스 추가 키를
한 곳에 모은 단일 settings. K-AIR 차용 모듈(query_runner/sql_guard/schemas)이
참조하는 키는 모두 동일 이름·동일 의미로 보존하여 그대로 import 가능.

- Neo4j (메타 그래프): NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD
- 원천 PostgreSQL (SQL 실행): SOURCE_PG_* + source_pg_schema
- OpenAI: HyDE + 임베딩
- decision 정책: K-AIR 와 동일 키
- /query/execute 정책: K-AIR 와 동일 키
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path, override=False)
except ImportError:
    pass


def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v is not None and v != "" else default


def _csv_set(name: str, default: str) -> frozenset[str]:
    raw = _env(name, default)
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


def _csv_list(name: str, default: str) -> tuple[str, ...]:
    raw = _env(name, default)
    return tuple(x.strip() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    # --- API ---
    api_host: str = _env("API_HOST", "0.0.0.0")
    api_port: int = int(_env("API_PORT", "8100"))

    # --- Neo4j (메타 그래프, robo-neo4j) ---
    neo4j_uri: str = _env("NEO4J_URI", "bolt://127.0.0.1:7687")
    neo4j_user: str = _env("NEO4J_USER", "neo4j")
    neo4j_password: str = _env("NEO4J_PASSWORD", "")

    # --- 원천 PostgreSQL (SQL 실행, robo-postgres) ---
    source_pg_host: str = _env("SOURCE_PG_HOST", "127.0.0.1")
    source_pg_port: int = int(_env("SOURCE_PG_PORT", "5432"))
    source_pg_db: str = _env("SOURCE_PG_DB", "rwis")
    source_pg_user: str = _env("SOURCE_PG_USER", "postgres")
    source_pg_pass: str = _env("SOURCE_PG_PASS", "")
    source_pg_schema: str = _env("SOURCE_PG_SCHEMA", "RWIS")

    # --- OpenAI ---
    openai_api_key: str = _env("OPENAI_API_KEY", "")
    openai_llm_model: str = _env("OPENAI_LLM_MODEL", "gpt-4o-mini")
    openai_embedding_model: str = _env("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    openai_embedding_dim: int = int(_env("OPENAI_EMBEDDING_DIM", "1536"))
    openai_base_url: str = _env("OPENAI_BASE_URL", "")
    openai_embedding_base_url: str = _env("OPENAI_EMBEDDING_BASE_URL", "")

    # --- decision 정책 (K-AIR 와 동일 키) ---
    decision_topk: int = int(_env("DECISION_VECTOR_TOPK", "10"))
    decision_score_gap_ratio: float = float(_env("DECISION_SCORE_GAP_RATIO", "0.85"))
    decision_score_min_step: float = float(_env("DECISION_SCORE_MIN_STEP", "0.012"))
    decision_score_top_radius: float = float(_env("DECISION_SCORE_TOP_RADIUS", "0.01"))
    decision_join_expand: bool = _env("DECISION_JOIN_EXPAND", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    )
    decision_join_expand_via: tuple[str, ...] = _csv_list("DECISION_JOIN_EXPAND_VIA", "fk")
    decision_table_max: int = int(_env("DECISION_TABLE_MAX", "0"))
    decision_column_top_m: int = int(_env("DECISION_COLUMN_TOP_M", "10"))
    decision_match_columns: bool = _env("DECISION_MATCH_COLUMNS", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    )
    decision_analytic_threshold: float = float(_env("DECISION_ANALYTIC_THRESHOLD", "0.70"))
    decision_source_threshold: float = float(_env("DECISION_SOURCE_THRESHOLD", "0.55"))
    hyde_weight: float = float(_env("HYDE_WEIGHT", "0.6"))
    question_weight: float = float(_env("QUESTION_WEIGHT", "0.4"))

    # --- join_groups 3단 fallback 정책 ---
    join_ontology_enabled: bool = _env("JOIN_ONTOLOGY_ENABLED", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    )
    join_convention_enabled: bool = _env("JOIN_CONVENTION_ENABLED", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    )
    join_convention_min_col_len: int = int(_env("JOIN_CONVENTION_MIN_COL_LEN", "4"))
    join_convention_confidence: float = float(_env("JOIN_CONVENTION_CONFIDENCE", "0.5"))
    join_convention_confidence_mismatch: float = float(
        _env("JOIN_CONVENTION_CONFIDENCE_MISMATCH", "0.35")
    )
    join_ontology_rel_types: tuple[str, ...] = _csv_list(
        "JOIN_ONTOLOGY_REL_TYPES",
        "fkToTable,FK_TO_TABLE,RELATED_TO,REFERENCES,DOMAIN_RELATED,dataFlowsTo",
    )
    join_convention_exclude: frozenset[str] = _csv_set(
        "JOIN_CONVENTION_EXCLUDE",
        "id,seq,yn,use_yn,del_yn,reg_dt,upd_dt,reg_id,upd_id,remark,memo,note",
    )
    join_fk_limit: int = int(_env("JOIN_FK_LIMIT", "50"))
    join_ontology_limit: int = int(_env("JOIN_ONTOLOGY_LIMIT", "50"))
    join_convention_limit: int = int(_env("JOIN_CONVENTION_LIMIT", "30"))
    neo4j_table_vector_index: str = _env(
        "NEO4J_TABLE_VECTOR_INDEX", "text_to_sql_table_vec_index"
    )
    decision_schema_allowlist: tuple[str, ...] = _csv_list("DECISION_SCHEMA_ALLOWLIST", "")

    # --- v0.7 entity resolution (A안) ---
    entity_resolution_enabled: bool = _env("ENTITY_RESOLUTION_ENABLED", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )
    entity_probe_datasource: str = _env("ENTITY_PROBE_DATASOURCE", "rwis")

    # --- meta_db 라벨 폴백 (R-3) ---
    meta_db_label: str = _env("META_DB_LABEL", "kwater_prod")

    # --- /query/execute 정책 (K-AIR 차용 query_runner 가 의존) ---
    exec_default_timeout_s: int = int(_env("EXEC_DEFAULT_TIMEOUT_S", "60"))
    exec_max_timeout_s: int = int(_env("EXEC_MAX_TIMEOUT_S", "300"))
    exec_default_max_rows: int = int(_env("EXEC_DEFAULT_MAX_ROWS", "1000"))
    exec_max_rows_hard: int = int(_env("EXEC_MAX_ROWS_HARD", "10000"))
    exec_max_bytes: int = int(_env("EXEC_MAX_BYTES", str(10 * 1024 * 1024)))
    exec_audit_log_path: str = _env(
        "EXEC_AUDIT_LOG_PATH",
        str(Path(__file__).parent.parent / "logs" / "query_audit.jsonl"),
    )

    @property
    def openai_enabled(self) -> bool:
        return bool(self.openai_api_key.strip())

    @property
    def source_dsn(self) -> str:
        return (
            f"host={self.source_pg_host} port={self.source_pg_port} "
            f"dbname={self.source_pg_db} user={self.source_pg_user} "
            f"password={self.source_pg_pass}"
        )


settings = Settings()
