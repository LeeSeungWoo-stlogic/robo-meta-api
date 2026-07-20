"""Server-side source binding and execution allowlist resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..runtime_config import SourceBindingRuntime, get_runtime
from .sql_guard import GuardError


class ExecutionBindingError(GuardError):
    """Raised when a client claim cannot be resolved to a server binding."""


def _engine(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return {
        "postgres": "postgresql",
        "postgresql": "postgresql",
        "tibero": "tibero",
        "oracle": "oracle",
    }.get(normalized, normalized)


def _claim_value(claimed: Mapping[str, Any], key: str) -> Any:
    value = claimed.get(key)
    if value is None or value == "":
        raise ExecutionBindingError(f"execution_context.{key}가 필요합니다")
    return value


@dataclass(frozen=True)
class ResolvedExecutionContext:
    source_instance_id: str
    backend: str
    integration: str
    catalog: str
    schema_name: str
    source_engine: str
    parser_dialect: str
    qualification_pattern: str
    identifier_quote: str
    require_quoted_uppercase_identifiers: bool
    allowed_catalogs: frozenset[str]
    allowed_schemas: frozenset[str]
    allowed_objects: frozenset[str]

    def public_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "dialect": self.source_engine,
            "integration": self.integration,
            "catalog": self.catalog,
            "schema_name": self.schema_name,
            "qualification_pattern": self.qualification_pattern,
            "identifier_quote": self.identifier_quote,
            "require_quoted_uppercase_identifiers": (
                self.require_quoted_uppercase_identifiers
            ),
            "source_instance_id": self.source_instance_id,
            "allowed_objects": sorted(self.allowed_objects),
        }

    def audit_dict(self) -> dict[str, Any]:
        return {
            **self.public_dict(),
            "parser_dialect": self.parser_dialect,
            "allowed_catalogs": sorted(self.allowed_catalogs),
            "allowed_schemas": sorted(self.allowed_schemas),
        }


def _validate_claim(
    claimed: Mapping[str, Any],
    *,
    binding: SourceBindingRuntime,
    backend: str,
    source_instance_id: str,
) -> None:
    expected = {
        "backend": backend,
        "dialect": binding.source_engine,
        "integration": binding.integration,
        "catalog": binding.catalog,
        "schema_name": binding.schema,
        "qualification_pattern": binding.qualification_pattern,
        "identifier_quote": binding.identifier_quote,
        "source_instance_id": source_instance_id,
        "require_quoted_uppercase_identifiers": (
            binding.require_quoted_uppercase_identifiers
        ),
    }
    for key, expected_value in expected.items():
        actual = _claim_value(claimed, key)
        if isinstance(expected_value, bool):
            matches = actual is expected_value
        else:
            matches = str(actual) == str(expected_value)
        if not matches:
            raise ExecutionBindingError(
                f"execution_context.{key}가 server binding과 다릅니다"
            )


async def resolve_execution_context(
    repository: Any,
    *,
    claimed_context: Mapping[str, Any] | None = None,
    source_instance_id: str | None = None,
    requested_objects: Iterable[str] | None = None,
    allow_default: bool = False,
) -> ResolvedExecutionContext:
    """Resolve all execution fields from trusted runtime and active metadata."""

    runtime = get_runtime()
    claimed_source = (
        str(claimed_context.get("source_instance_id") or "").strip()
        if claimed_context
        else ""
    )
    if source_instance_id and claimed_source and source_instance_id != claimed_source:
        raise ExecutionBindingError("서로 다른 source_instance_id가 혼합되었습니다")
    selected_source = source_instance_id or claimed_source
    if not selected_source and allow_default:
        selected_source = runtime.execution.default_source_instance_id
    if not selected_source:
        raise ExecutionBindingError("source_instance_id를 결정할 수 없습니다")
    try:
        binding = runtime.execution.binding_for(selected_source)
    except Exception as exc:
        raise ExecutionBindingError(str(exc)) from exc

    state = await repository.execution_source_scope(selected_source)
    if state is None:
        raise ExecutionBindingError(
            f"Metadata Store에 datasource가 없습니다: {selected_source}"
        )
    if _engine(state.get("engine")) != _engine(binding.source_engine):
        raise ExecutionBindingError("Metadata Store engine과 server binding이 다릅니다")
    source_schema = str(state.get("source_schema") or "")
    if source_schema.lower() != binding.schema.lower():
        raise ExecutionBindingError("Metadata Store schema와 server binding이 다릅니다")

    legacy_integration = str(state.get("mindsdb_integration") or "")
    legacy_catalog = str(state.get("mindsdb_catalog") or "")
    if legacy_integration and legacy_integration != binding.integration:
        raise ExecutionBindingError(
            "legacy mindsdb_integration과 server binding이 다릅니다"
        )
    if legacy_catalog and legacy_catalog != binding.catalog:
        raise ExecutionBindingError(
            "legacy mindsdb_catalog과 server binding이 다릅니다"
        )

    active_by_key = {
        str(item).lower(): str(item)
        for item in state.get("allowed_objects") or []
        if str(item).strip()
    }
    if not active_by_key:
        raise ExecutionBindingError(
            f"활성·승인 execution object가 없습니다: {selected_source}"
        )

    if claimed_context is not None:
        _validate_claim(
            claimed_context,
            binding=binding,
            backend=runtime.execution.backend,
            source_instance_id=selected_source,
        )
    requested = {
        str(item).lower()
        for item in (requested_objects or [])
        if str(item).strip()
    }
    claimed_objects = {
        str(item).lower()
        for item in (
            claimed_context.get("allowed_objects", [])
            if claimed_context is not None
            else []
        )
        if str(item).strip()
    }
    for label, values in (
        ("requested_objects", requested),
        ("execution_context.allowed_objects", claimed_objects),
    ):
        unknown = sorted(values - set(active_by_key))
        if unknown:
            raise ExecutionBindingError(
                f"{label}에 미등록·비활성 object가 있습니다: {', '.join(unknown)}"
            )
    if requested and claimed_objects and requested != claimed_objects:
        raise ExecutionBindingError("요청 object와 execution_context가 다릅니다")
    effective_keys = requested or claimed_objects or set(active_by_key)
    return ResolvedExecutionContext(
        source_instance_id=selected_source,
        backend=runtime.execution.backend,
        integration=binding.integration,
        catalog=binding.catalog,
        schema_name=binding.schema,
        source_engine=binding.source_engine,
        parser_dialect=binding.parser_dialect,
        qualification_pattern=binding.qualification_pattern,
        identifier_quote=binding.identifier_quote,
        require_quoted_uppercase_identifiers=(
            binding.require_quoted_uppercase_identifiers
        ),
        allowed_catalogs=binding.allowed_catalogs,
        allowed_schemas=binding.allowed_schemas,
        allowed_objects=frozenset(active_by_key[key] for key in effective_keys),
    )


async def validate_runtime_bindings(repository: Any) -> list[dict[str, Any]]:
    """Fail startup when configured bindings cannot resolve to active metadata."""

    runtime = get_runtime()
    results: list[dict[str, Any]] = []
    for source_instance_id in runtime.execution.source_bindings:
        resolved = await resolve_execution_context(
            repository,
            source_instance_id=source_instance_id,
        )
        results.append(
            {
                "source_instance_id": source_instance_id,
                "integration": resolved.integration,
                "active_object_count": len(resolved.allowed_objects),
            }
        )
    return results
