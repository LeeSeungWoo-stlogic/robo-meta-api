from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class RuntimeConfigError(RuntimeError):
    pass


_FORBIDDEN_EXECUTION_KEYS = frozenset(
    {
        "source_bindings",
        "default_source_instance_id",
        "integration",
        "catalog",
        "schema",
        "dialect",
        "qualification_pattern",
        "identifier_quote",
        "require_quoted_uppercase_identifiers",
        "allowed_catalogs",
        "allowed_schemas",
    }
)


def _required(mapping: dict[str, Any], path: str) -> Any:
    current: Any = mapping
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            raise RuntimeConfigError(f"Missing required runtime setting: {path}")
        current = current[key]
    if current is None or current == "":
        raise RuntimeConfigError(f"Empty required runtime setting: {path}")
    return current


def _optional(mapping: dict[str, Any], path: str, default: Any) -> Any:
    current: Any = mapping
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return default if current is None or current == "" else current


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _secret(reference: dict[str, Any]) -> str:
    provider = _required(reference, "provider")
    if provider != "process_env":
        raise RuntimeConfigError(f"Unsupported robo-meta-api secret provider: {provider}")
    key = str(_required(reference, "env_key"))
    value = os.environ.get(key, "")
    if not value:
        raise RuntimeConfigError(f"Secret environment variable is empty: {key}")
    return value


def _reject_hardcoded_source_execution(execution: dict[str, Any]) -> None:
    """Fail closed when YAML still registers sources (platform Store is SoT)."""
    present = sorted(key for key in _FORBIDDEN_EXECUTION_KEYS if key in execution)
    if present:
        raise RuntimeConfigError(
            "execution must not hardcode registered sources; "
            f"remove keys: {', '.join(present)}"
        )


@dataclass(frozen=True)
class MetadataRuntime:
    host: str
    port: int
    database: str
    schema: str
    user: str
    password: str

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}@{self.host}:"
            f"{self.port}/{self.database}"
        )


@dataclass(frozen=True)
class EmbeddingRuntime:
    base_url: str
    model: str
    dimensions: int
    auth_mode: str
    api_key: str | None
    timeout_seconds: float
    provider: str = "http"


@dataclass(frozen=True)
class DecisionRuntime:
    table_top_k: int
    column_top_m: int
    minimum_similarity: float
    verified_join_confidence: float
    convention_join_confidence: float
    hyde_enabled: bool = True
    hyde_model: str = "gpt-4o-mini"
    hyde_weight: float = 0.6
    question_weight: float = 0.4
    role_weight: float = 0.35
    role_top_k: int = 3
    role_min_score_ratio: float = 0.8
    role_semantic_floor: float = 0.55
    score_gap_ratio: float = 0.85
    score_min_step: float = 0.012
    score_top_radius: float = 0.01
    fk_max_hops: int = 3
    fk_path_limit: int = 50
    analysis_base_url: str | None = None
    analysis_timeout_seconds: float = 15
    analysis_reasoning_effort: bool = False


@dataclass(frozen=True)
class SourceBindingRuntime:
    source_instance_id: str
    integration: str
    catalog: str
    schema: str
    source_engine: str
    parser_dialect: str
    qualification_pattern: str
    identifier_quote: str
    require_quoted_uppercase_identifiers: bool
    allowed_catalogs: frozenset[str]
    allowed_schemas: frozenset[str]


@dataclass(frozen=True)
class ExecutionRuntime:
    """Infrastructure-only execution settings. Source identity comes from Store."""

    backend: str
    sql_api_url: str
    default_timeout_seconds: int
    maximum_timeout_seconds: int
    default_max_rows: int
    maximum_rows: int
    maximum_response_bytes: int
    audit_log_path: str


@dataclass(frozen=True)
class T2SqlRuntime:
    model: str | None
    base_url: str | None
    max_probe_steps: int = 4
    probe_timeout_seconds: int = 8
    probe_row_limit: int = 20
    generate_timeout_seconds: int = 30
    max_generate_retries: int = 1
    validate_max_rows: int = 5
    total_timeout_seconds: int = 60
    expose_draft_sql: bool = False

    def configured(self) -> bool:
        return bool(self.model) and bool(self.base_url)


@dataclass(frozen=True)
class RoboRuntime:
    settings_path: Path
    api_host: str
    api_port: int
    metadata_backend: str
    metadata: MetadataRuntime
    embedding: EmbeddingRuntime
    decision: DecisionRuntime
    execution: ExecutionRuntime
    t2sql: T2SqlRuntime | None = None


_runtime: RoboRuntime | None = None


def _load_t2sql(
    robo: dict[str, Any],
    *,
    embedding_base_url: str,
    analysis_base_url: str | None,
    maximum_timeout_seconds: int,
    maximum_rows: int,
) -> T2SqlRuntime:
    raw = robo.get("t2sql")
    mapping = raw if isinstance(raw, dict) else {}
    env_model = str(os.environ.get("T2SQL_LLM_MODEL") or "").strip() or None
    yaml_model = str(mapping.get("model") or "").strip() or None
    yaml_base = str(mapping.get("base_url") or "").strip() or None
    base_url = yaml_base or analysis_base_url or embedding_base_url or None
    probe_timeout = int(_optional(mapping, "probe_timeout_seconds", 8))
    if probe_timeout > maximum_timeout_seconds:
        raise RuntimeConfigError(
            "t2sql.probe_timeout_seconds must be <= execution.maximum_timeout_seconds"
        )
    validate_max_rows = int(_optional(mapping, "validate_max_rows", 5))
    if validate_max_rows > maximum_rows:
        raise RuntimeConfigError(
            "t2sql.validate_max_rows must be <= execution.maximum_rows"
        )
    total_timeout = int(_optional(mapping, "total_timeout_seconds", 60))
    if total_timeout < 1 or total_timeout > 120:
        raise RuntimeConfigError("t2sql.total_timeout_seconds must be 1..120")
    return T2SqlRuntime(
        model=env_model or yaml_model,
        base_url=base_url,
        max_probe_steps=int(_optional(mapping, "max_probe_steps", 4)),
        probe_timeout_seconds=probe_timeout,
        probe_row_limit=int(_optional(mapping, "probe_row_limit", 20)),
        generate_timeout_seconds=int(_optional(mapping, "generate_timeout_seconds", 30)),
        max_generate_retries=int(_optional(mapping, "max_generate_retries", 1)),
        validate_max_rows=validate_max_rows,
        total_timeout_seconds=total_timeout,
        expose_draft_sql=bool(mapping.get("expose_draft_sql") or False),
    )


def load_runtime(path: str | Path) -> RoboRuntime:
    settings_path = Path(path).resolve()
    payload = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeConfigError("Runtime settings root must be a mapping")
    robo = _required(payload, "robo_meta_api")
    metadata = _required(robo, "metadata_store")
    embedding = _required(robo, "embedding")
    decision = _required(robo, "decision")
    execution = _required(robo, "execution")
    if not isinstance(execution, dict):
        raise RuntimeConfigError("execution must be a mapping")
    _reject_hardcoded_source_execution(execution)
    auth_mode = str(_required(embedding, "auth_mode"))
    api_key = (
        _secret(_required(embedding, "api_key_ref"))
        if auth_mode == "bearer"
        else None
    )
    analysis_base_url = (
        str(_optional(decision, "analysis_base_url", "")).strip() or None
    )
    embedding_base_url = str(_required(embedding, "base_url"))
    maximum_timeout_seconds = int(_required(execution, "maximum_timeout_seconds"))
    maximum_rows = int(_required(execution, "maximum_rows"))
    t2sql = _load_t2sql(
        robo,
        embedding_base_url=embedding_base_url,
        analysis_base_url=analysis_base_url,
        maximum_timeout_seconds=maximum_timeout_seconds,
        maximum_rows=maximum_rows,
    )
    runtime = RoboRuntime(
        settings_path=settings_path,
        api_host=str(_required(robo, "api_host")),
        api_port=int(_required(robo, "api_port")),
        metadata_backend=str(_required(robo, "metadata_backend")),
        metadata=MetadataRuntime(
            host=str(_required(metadata, "host")),
            port=int(_required(metadata, "port")),
            database=str(_required(metadata, "database")),
            schema=str(_required(metadata, "schema")),
            user=str(_required(metadata, "user")),
            password=_secret(_required(metadata, "password_ref")),
        ),
        embedding=EmbeddingRuntime(
            base_url=str(_required(embedding, "base_url")),
            model=str(_required(embedding, "model")),
            dimensions=int(_required(embedding, "dimensions")),
            auth_mode=auth_mode,
            api_key=api_key,
            timeout_seconds=float(_required(embedding, "timeout_seconds")),
            provider=str(_optional(embedding, "provider", "http")),
        ),
        decision=DecisionRuntime(
            table_top_k=int(_required(decision, "table_top_k")),
            column_top_m=int(_required(decision, "column_top_m")),
            minimum_similarity=float(_required(decision, "minimum_similarity")),
            verified_join_confidence=float(
                _required(decision, "verified_join_confidence")
            ),
            convention_join_confidence=float(
                _required(decision, "convention_join_confidence")
            ),
            hyde_enabled=_as_bool(_optional(decision, "hyde_enabled", True)),
            hyde_model=str(_optional(decision, "hyde_model", "gpt-4o-mini")),
            hyde_weight=float(_optional(decision, "hyde_weight", 0.6)),
            question_weight=float(_optional(decision, "question_weight", 0.4)),
            role_weight=float(_optional(decision, "role_weight", 0.35)),
            role_top_k=int(_optional(decision, "role_top_k", 3)),
            role_min_score_ratio=float(
                _optional(decision, "role_min_score_ratio", 0.8)
            ),
            role_semantic_floor=float(
                _optional(decision, "role_semantic_floor", 0.55)
            ),
            score_gap_ratio=float(_optional(decision, "score_gap_ratio", 0.85)),
            score_min_step=float(_optional(decision, "score_min_step", 0.012)),
            score_top_radius=float(_optional(decision, "score_top_radius", 0.01)),
            fk_max_hops=int(_optional(decision, "fk_max_hops", 3)),
            fk_path_limit=int(_optional(decision, "fk_path_limit", 50)),
            analysis_base_url=analysis_base_url,
            analysis_timeout_seconds=float(
                _optional(decision, "analysis_timeout_seconds", 15)
            ),
            analysis_reasoning_effort=_as_bool(
                _optional(decision, "analysis_reasoning_effort", False)
            ),
        ),
        execution=ExecutionRuntime(
            backend=str(_required(execution, "backend")),
            sql_api_url=str(_required(execution, "sql_api_url")),
            default_timeout_seconds=int(
                _required(execution, "default_timeout_seconds")
            ),
            maximum_timeout_seconds=maximum_timeout_seconds,
            default_max_rows=int(_required(execution, "default_max_rows")),
            maximum_rows=maximum_rows,
            maximum_response_bytes=int(
                _required(execution, "maximum_response_bytes")
            ),
            audit_log_path=str(_required(execution, "audit_log_path")),
        ),
        t2sql=t2sql,
    )
    if runtime.metadata_backend != "postgres":
        raise RuntimeConfigError("Part 2 requires metadata_backend=postgres")
    if runtime.execution.backend != "mindsdb":
        raise RuntimeConfigError("Part 2 requires execution.backend=mindsdb")
    if runtime.embedding.auth_mode not in {"none", "bearer"}:
        raise RuntimeConfigError("Unsupported embedding auth_mode")
    if runtime.embedding.provider not in {"http", "lexical_test"}:
        raise RuntimeConfigError("Unsupported embedding.provider")
    if runtime.embedding.provider == "lexical_test" and (
        runtime.embedding.model != "lexical-hash-test"
        or runtime.embedding.dimensions != 1024
        or runtime.embedding.auth_mode != "none"
    ):
        raise RuntimeConfigError(
            "lexical_test provider requires model=lexical-hash-test, "
            "dimensions=1024, auth_mode=none"
        )
    return runtime


def init_runtime(path: str | Path | None = None) -> RoboRuntime:
    global _runtime
    if _runtime is None:
        try:
            from dotenv import load_dotenv

            env_path = Path(__file__).resolve().parents[1] / ".env"
            if env_path.exists():
                load_dotenv(env_path, override=False)
        except ImportError:
            pass
        selected = path or os.environ.get("ROBO_RUNTIME_SETTINGS_FILE")
        if not selected:
            raise RuntimeConfigError("ROBO_RUNTIME_SETTINGS_FILE is required")
        _runtime = load_runtime(selected)
    return _runtime


def get_runtime() -> RoboRuntime:
    if _runtime is None:
        raise RuntimeConfigError("robo-meta-api runtime is not initialized")
    return _runtime


def reset_runtime_for_tests() -> None:
    global _runtime
    _runtime = None
