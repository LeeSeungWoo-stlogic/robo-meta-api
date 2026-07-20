from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class RuntimeConfigError(RuntimeError):
    pass


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


@dataclass(frozen=True)
class ExecutionRuntime:
    backend: str
    sql_api_url: str
    integration: str
    catalog: str
    schema: str
    dialect: str
    qualification_pattern: str
    identifier_quote: str
    require_quoted_uppercase_identifiers: bool
    allowed_catalogs: frozenset[str]
    allowed_schemas: frozenset[str]
    default_timeout_seconds: int
    maximum_timeout_seconds: int
    default_max_rows: int
    maximum_rows: int
    maximum_response_bytes: int
    audit_log_path: str


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


_runtime: RoboRuntime | None = None


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
    auth_mode = str(_required(embedding, "auth_mode"))
    api_key = (
        _secret(_required(embedding, "api_key_ref"))
        if auth_mode == "bearer"
        else None
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
            hyde_enabled=_as_bool(
                _optional(decision, "hyde_enabled", True)
            ),
            hyde_model=str(
                _optional(decision, "hyde_model", "gpt-4o-mini")
            ),
            hyde_weight=float(
                _optional(decision, "hyde_weight", 0.6)
            ),
            question_weight=float(
                _optional(decision, "question_weight", 0.4)
            ),
            role_weight=float(
                _optional(decision, "role_weight", 0.35)
            ),
            role_top_k=int(
                _optional(decision, "role_top_k", 3)
            ),
            role_min_score_ratio=float(
                _optional(decision, "role_min_score_ratio", 0.8)
            ),
            role_semantic_floor=float(
                _optional(decision, "role_semantic_floor", 0.55)
            ),
            score_gap_ratio=float(
                _optional(decision, "score_gap_ratio", 0.85)
            ),
            score_min_step=float(
                _optional(decision, "score_min_step", 0.012)
            ),
            score_top_radius=float(
                _optional(decision, "score_top_radius", 0.01)
            ),
            fk_max_hops=int(
                _optional(decision, "fk_max_hops", 3)
            ),
            fk_path_limit=int(
                _optional(decision, "fk_path_limit", 50)
            ),
            analysis_base_url=(
                str(_optional(decision, "analysis_base_url", "")).strip()
                or None
            ),
        ),
        execution=ExecutionRuntime(
            backend=str(_required(execution, "backend")),
            sql_api_url=str(_required(execution, "sql_api_url")),
            integration=str(_required(execution, "integration")),
            catalog=str(_required(execution, "catalog")),
            schema=str(_required(execution, "schema")),
            dialect=str(_required(execution, "dialect")),
            qualification_pattern=str(_required(execution, "qualification_pattern")),
            identifier_quote=str(_required(execution, "identifier_quote")),
            require_quoted_uppercase_identifiers=bool(
                _required(execution, "require_quoted_uppercase_identifiers")
            ),
            allowed_catalogs=frozenset(
                str(item).lower()
                for item in _required(execution, "allowed_catalogs")
            ),
            allowed_schemas=frozenset(
                str(item).lower()
                for item in _required(execution, "allowed_schemas")
            ),
            default_timeout_seconds=int(
                _required(execution, "default_timeout_seconds")
            ),
            maximum_timeout_seconds=int(
                _required(execution, "maximum_timeout_seconds")
            ),
            default_max_rows=int(_required(execution, "default_max_rows")),
            maximum_rows=int(_required(execution, "maximum_rows")),
            maximum_response_bytes=int(
                _required(execution, "maximum_response_bytes")
            ),
            audit_log_path=str(_required(execution, "audit_log_path")),
        ),
    )
    if runtime.metadata_backend != "postgres":
        raise RuntimeConfigError("Part 2 requires metadata_backend=postgres")
    if runtime.execution.backend != "mindsdb":
        raise RuntimeConfigError("Part 2 requires execution.backend=mindsdb")
    if runtime.embedding.auth_mode not in {"none", "bearer"}:
        raise RuntimeConfigError("Unsupported embedding auth_mode")
    return runtime


def init_runtime(path: str | Path | None = None) -> RoboRuntime:
    global _runtime
    if _runtime is None:
        selected = path or os.environ.get("ROBO_RUNTIME_SETTINGS_FILE")
        if not selected:
            raise RuntimeConfigError("ROBO_RUNTIME_SETTINGS_FILE is required")
        _runtime = load_runtime(selected)
    return _runtime


def get_runtime() -> RoboRuntime:
    if _runtime is None:
        raise RuntimeConfigError("robo-meta-api runtime is not initialized")
    return _runtime
