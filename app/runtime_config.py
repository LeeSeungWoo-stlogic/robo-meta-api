from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

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


def _source_bindings(
    execution: dict[str, Any],
) -> tuple[Mapping[str, SourceBindingRuntime], str]:
    raw = _required(execution, "source_bindings")
    if not isinstance(raw, dict) or not raw:
        raise RuntimeConfigError("execution.source_bindings must be a non-empty mapping")
    default_id = str(_required(execution, "default_source_instance_id"))
    bindings: dict[str, SourceBindingRuntime] = {}
    for raw_id, raw_binding in raw.items():
        source_id = str(raw_id).strip()
        if not source_id or not isinstance(raw_binding, dict):
            raise RuntimeConfigError("source binding key/value가 올바르지 않습니다")
        integration = str(_required(raw_binding, "integration"))
        catalog = str(_required(raw_binding, "catalog"))
        schema = str(_required(raw_binding, "schema"))
        allowed_catalogs = frozenset(
            str(item).lower()
            for item in _optional(
                raw_binding,
                "allowed_catalogs",
                [integration],
            )
        )
        allowed_schemas = frozenset(
            str(item).lower()
            for item in _optional(
                raw_binding,
                "allowed_schemas",
                [schema],
            )
        )
        if integration.lower() not in allowed_catalogs:
            raise RuntimeConfigError(
                f"source binding {source_id}: integration이 allowed_catalogs에 없습니다"
            )
        if schema.lower() not in allowed_schemas:
            raise RuntimeConfigError(
                f"source binding {source_id}: schema가 allowed_schemas에 없습니다"
            )
        bindings[source_id] = SourceBindingRuntime(
            source_instance_id=source_id,
            integration=integration,
            catalog=catalog,
            schema=schema,
            source_engine=str(_required(raw_binding, "source_engine")),
            parser_dialect=str(_required(raw_binding, "parser_dialect")),
            qualification_pattern=str(
                _required(raw_binding, "qualification_pattern")
            ),
            identifier_quote=str(_required(raw_binding, "identifier_quote")),
            require_quoted_uppercase_identifiers=_as_bool(
                _required(
                    raw_binding,
                    "require_quoted_uppercase_identifiers",
                )
            ),
            allowed_catalogs=allowed_catalogs,
            allowed_schemas=allowed_schemas,
        )
    if default_id not in bindings:
        raise RuntimeConfigError(
            "execution.default_source_instance_id가 source_bindings에 없습니다"
        )
    return MappingProxyType(bindings), default_id


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
    source_bindings: Mapping[str, SourceBindingRuntime] = field(
        default_factory=lambda: MappingProxyType({})
    )
    default_source_instance_id: str | None = None

    def binding_for(
        self,
        source_instance_id: str | None = None,
    ) -> SourceBindingRuntime:
        selected = source_instance_id or self.default_source_instance_id
        if self.source_bindings:
            if not selected or selected not in self.source_bindings:
                raise RuntimeConfigError(
                    f"등록되지 않은 source_instance_id: {selected or '<empty>'}"
                )
            return self.source_bindings[selected]
        # 수동으로 ExecutionRuntime을 만드는 기존 단위시험만을 위한 호환 경로다.
        legacy_id = selected or "legacy-default"
        return SourceBindingRuntime(
            source_instance_id=legacy_id,
            integration=self.integration,
            catalog=self.catalog,
            schema=self.schema,
            source_engine=self.dialect,
            parser_dialect=self.dialect,
            qualification_pattern=self.qualification_pattern,
            identifier_quote=self.identifier_quote,
            require_quoted_uppercase_identifiers=(
                self.require_quoted_uppercase_identifiers
            ),
            allowed_catalogs=self.allowed_catalogs,
            allowed_schemas=self.allowed_schemas,
        )


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
    bindings, default_binding_id = _source_bindings(execution)
    default_binding = bindings[default_binding_id]
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
            integration=default_binding.integration,
            catalog=default_binding.catalog,
            schema=default_binding.schema,
            dialect=default_binding.parser_dialect,
            qualification_pattern=default_binding.qualification_pattern,
            identifier_quote=default_binding.identifier_quote,
            require_quoted_uppercase_identifiers=(
                default_binding.require_quoted_uppercase_identifiers
            ),
            allowed_catalogs=default_binding.allowed_catalogs,
            allowed_schemas=default_binding.allowed_schemas,
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
            source_bindings=bindings,
            default_source_instance_id=default_binding_id,
        ),
    )
    if runtime.metadata_backend != "postgres":
        raise RuntimeConfigError("Part 2 requires metadata_backend=postgres")
    if runtime.execution.backend != "mindsdb":
        raise RuntimeConfigError("Part 2 requires execution.backend=mindsdb")
    if runtime.embedding.auth_mode not in {"none", "bearer"}:
        raise RuntimeConfigError("Unsupported embedding auth_mode")
    if runtime.embedding.provider not in {"http", "lexical_test"}:
        raise RuntimeConfigError("Unsupported embedding.provider")
    if (
        runtime.embedding.provider == "lexical_test"
        and (
            runtime.embedding.model != "lexical-hash-test"
            or runtime.embedding.dimensions != 1024
            or runtime.embedding.auth_mode != "none"
        )
    ):
        raise RuntimeConfigError(
            "lexical_test provider requires model=lexical-hash-test, "
            "dimensions=1024, auth_mode=none"
        )
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
