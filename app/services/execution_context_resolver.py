"""Server-side source binding from Metadata Store (no YAML source registry)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..runtime_config import SourceBindingRuntime, get_runtime
from .sql_guard import GuardError


class ExecutionBindingError(GuardError):
    """Raised when a client claim cannot be resolved to a Store binding."""


# MindsDB SQL shaping by source engine — not a source registration map.
_MINDSDB_SQL_POLICY: dict[str, dict[str, Any]] = {
    "postgresql": {
        "parser_dialect": "mysql",
        "qualification_pattern": "{catalog}.{table}",
        "identifier_quote": "`",
        "require_quoted_uppercase_identifiers": False,
    },
    "tibero": {
        "parser_dialect": "mysql",
        "qualification_pattern": "{catalog}.{table}",
        "identifier_quote": "`",
        "require_quoted_uppercase_identifiers": True,
    },
    "oracle": {
        "parser_dialect": "mysql",
        "qualification_pattern": "{catalog}.{table}",
        "identifier_quote": "`",
        "require_quoted_uppercase_identifiers": True,
    },
}


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


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _norm_lower(value: Any) -> str:
    return _norm(value).lower()


def _parse_object_refs(raw: Any) -> frozenset[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    for item in raw or []:
        if isinstance(item, Mapping):
            schema = _norm(item.get("schema_name"))
            table = _norm(item.get("original_name") or item.get("table_name"))
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            schema = _norm(item[0])
            table = _norm(item[1])
        else:
            continue
        if schema and table:
            refs.add((schema, table))
    return frozenset(refs)


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
    source_name: str | None = None
    allowed_object_refs: frozenset[tuple[str, str]] = frozenset()

    def public_dict(self) -> dict[str, Any]:
        """API-facing fields. catalog/integration = platform source_name."""

        if not self.source_name:
            raise ExecutionBindingError(
                "source_name이 없어 public execution_context를 만들 수 없습니다"
            )
        return {
            "backend": self.backend,
            "dialect": self.source_engine,
            "integration": self.source_name,
            "catalog": self.source_name,
            "schema_name": self.schema_name,
            "qualification_pattern": self.qualification_pattern,
            "identifier_quote": self.identifier_quote,
            "require_quoted_uppercase_identifiers": (
                self.require_quoted_uppercase_identifiers
            ),
            "source_instance_id": self.source_instance_id,
            "source_name": self.source_name,
            "allowed_objects": sorted(self.allowed_objects),
        }

    def audit_dict(self) -> dict[str, Any]:
        """Internal/audit fields. catalog/integration = mindsdb_* (uncontaminated)."""

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
            "source_name": self.source_name,
            "allowed_objects": sorted(self.allowed_objects),
            "parser_dialect": self.parser_dialect,
            "allowed_catalogs": sorted(self.allowed_catalogs),
            "allowed_schemas": sorted(self.allowed_schemas),
        }


def binding_from_store_row(state: Mapping[str, Any]) -> SourceBindingRuntime:
    """Assemble a binding from one t2s_datasources scope row."""

    source_instance_id = str(state.get("source_instance_id") or "").strip()
    if not source_instance_id:
        raise ExecutionBindingError("Metadata Store row에 source_instance_id가 없습니다")
    integration = str(state.get("mindsdb_integration") or "").strip()
    catalog = str(state.get("mindsdb_catalog") or "").strip()
    if not integration or not catalog:
        raise ExecutionBindingError(
            f"Metadata Store mindsdb_integration/catalog가 비어 있습니다: "
            f"{source_instance_id}"
        )
    if integration.lower() != catalog.lower():
        raise ExecutionBindingError(
            "mindsdb_integration과 mindsdb_catalog가 일치하지 않습니다"
        )
    schema = str(state.get("source_schema") or "").strip()
    if not schema:
        raise ExecutionBindingError(
            f"Metadata Store source_schema가 비어 있습니다: {source_instance_id}"
        )
    source_engine = _engine(state.get("engine"))
    policy = _MINDSDB_SQL_POLICY.get(source_engine)
    if policy is None:
        raise ExecutionBindingError(f"지원하지 않는 source engine: {source_engine}")
    catalog_key = catalog.lower()
    distinct_schemas = frozenset(
        _norm_lower(item)
        for item in (state.get("allowed_schemas") or [])
        if _norm(item)
    )
    return SourceBindingRuntime(
        source_instance_id=source_instance_id,
        integration=integration,
        catalog=catalog,
        schema=schema,
        source_engine=source_engine,
        parser_dialect=str(policy["parser_dialect"]),
        qualification_pattern=str(policy["qualification_pattern"]),
        identifier_quote=str(policy["identifier_quote"]),
        require_quoted_uppercase_identifiers=bool(
            policy["require_quoted_uppercase_identifiers"]
        ),
        allowed_catalogs=frozenset({catalog_key}),
        allowed_schemas=distinct_schemas,
    )


def _validate_claim(
    claimed: Mapping[str, Any],
    *,
    binding: SourceBindingRuntime,
    backend: str,
    source_instance_id: str,
    source_name: str | None,
) -> None:
    expected_exact = {
        "backend": backend,
        "dialect": binding.source_engine,
        "schema_name": binding.schema,
        "qualification_pattern": binding.qualification_pattern,
        "identifier_quote": binding.identifier_quote,
        "source_instance_id": source_instance_id,
        "require_quoted_uppercase_identifiers": (
            binding.require_quoted_uppercase_identifiers
        ),
    }
    for key, expected_value in expected_exact.items():
        actual = _claim_value(claimed, key)
        if isinstance(expected_value, bool):
            matches = actual is expected_value
        else:
            matches = str(actual) == str(expected_value)
        if not matches:
            raise ExecutionBindingError(
                f"execution_context.{key}가 server binding과 다릅니다"
            )

    accepted_catalogs = {binding.catalog.lower(), binding.integration.lower()}
    if source_name:
        accepted_catalogs.add(source_name.lower())
    for key in ("catalog", "integration"):
        actual = _norm_lower(_claim_value(claimed, key))
        if actual not in accepted_catalogs:
            raise ExecutionBindingError(
                f"execution_context.{key}가 server binding과 다릅니다"
            )

    claimed_source_name = claimed.get("source_name")
    if claimed_source_name is not None and str(claimed_source_name).strip():
        if not source_name or _norm_lower(claimed_source_name) != source_name.lower():
            raise ExecutionBindingError(
                "execution_context.source_name이 server binding과 다릅니다"
            )


async def resolve_by_source_name(repository: Any, name: str) -> str:
    """Map platform SourceName → single source_instance_id."""

    normalized = _norm(name)
    if not normalized:
        raise ExecutionBindingError("source_name이 비어 있습니다")
    finder = getattr(repository, "find_profile_ids_by_source_name", None)
    if finder is None:
        raise ExecutionBindingError("repository가 source_name resolve를 지원하지 않습니다")
    matches = await finder(normalized)
    if len(matches) == 0:
        raise ExecutionBindingError(f"source_name을 찾을 수 없습니다: {normalized}")
    if len(matches) > 1:
        raise ExecutionBindingError(
            f"source_name이 모호합니다(복수 매칭): {normalized}"
        )
    return matches[0]


async def resolve_by_mindsdb_catalog(repository: Any, catalog: str) -> str:
    """Map mindsdb_catalog → single source_instance_id (kair_* 하위호환)."""

    normalized = _norm(catalog)
    if not normalized:
        raise ExecutionBindingError("mindsdb catalog가 비어 있습니다")
    finder = getattr(repository, "find_profile_ids_by_mindsdb_catalog", None)
    if finder is None:
        raise ExecutionBindingError(
            "repository가 mindsdb catalog resolve를 지원하지 않습니다"
        )
    matches = await finder(normalized)
    if len(matches) == 0:
        raise ExecutionBindingError(
            f"mindsdb catalog를 찾을 수 없습니다: {normalized}"
        )
    if len(matches) > 1:
        raise ExecutionBindingError(
            f"mindsdb catalog가 모호합니다(복수 매칭): {normalized}"
        )
    return matches[0]


async def resolve_execution_context(
    repository: Any,
    *,
    claimed_context: Mapping[str, Any] | None = None,
    source_instance_id: str | None = None,
    source_name: str | None = None,
    sql_source_key: str | None = None,
    requested_objects: Iterable[str] | None = None,
    allow_sql_source_resolve: bool = False,
) -> ResolvedExecutionContext:
    """Resolve execution fields from Store for an explicit source_instance_id."""

    runtime = get_runtime()
    claimed_source = (
        str(claimed_context.get("source_instance_id") or "").strip()
        if claimed_context
        else ""
    )
    claimed_name = (
        _norm(claimed_context.get("source_name"))
        if claimed_context
        else ""
    )
    selected_source = (source_instance_id or claimed_source or "").strip()
    selected_name = (source_name or claimed_name or "").strip()

    if source_instance_id and claimed_source and source_instance_id != claimed_source:
        raise ExecutionBindingError("서로 다른 source_instance_id가 혼합되었습니다")

    if not selected_source and selected_name:
        selected_source = await resolve_by_source_name(repository, selected_name)

    if not selected_source and allow_sql_source_resolve and sql_source_key:
        key = _norm(sql_source_key)
        try:
            selected_source = await resolve_by_source_name(repository, key)
        except ExecutionBindingError:
            selected_source = await resolve_by_mindsdb_catalog(repository, key)

    if not selected_source:
        raise ExecutionBindingError("source_instance_id를 결정할 수 없습니다")

    state = await repository.execution_source_scope(selected_source)
    if state is None:
        raise ExecutionBindingError(
            f"Metadata Store에 datasource가 없습니다: {selected_source}"
        )
    binding = binding_from_store_row(state)
    store_source_name = _norm(state.get("source_name")) or None

    if selected_name and store_source_name:
        if selected_name.lower() != store_source_name.lower():
            raise ExecutionBindingError(
                "source_name이 Metadata Store binding과 다릅니다"
            )
    if sql_source_key and allow_sql_source_resolve:
        key_lower = _norm_lower(sql_source_key)
        accepted = {binding.catalog.lower()}
        if store_source_name:
            accepted.add(store_source_name.lower())
        if key_lower not in accepted:
            raise ExecutionBindingError(
                "SQL 수식 소스가 execution_context/Store와 다릅니다"
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
    object_refs = _parse_object_refs(state.get("allowed_object_refs"))
    if not object_refs:
        raise ExecutionBindingError(
            f"활성·승인 execution object ref가 없습니다: {selected_source}"
        )
    if not binding.allowed_schemas:
        raise ExecutionBindingError(
            f"활성·승인 schema가 없습니다: {selected_source}"
        )

    if claimed_context is not None:
        _validate_claim(
            claimed_context,
            binding=binding,
            backend=runtime.execution.backend,
            source_instance_id=selected_source,
            source_name=store_source_name,
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
        source_name=store_source_name,
        allowed_object_refs=object_refs,
    )


async def validate_runtime_bindings(repository: Any) -> list[dict[str, Any]]:
    """Boot check: Store reachable. Empty/multi source both OK."""

    sources = await repository.list_execution_sources()
    return [
        {
            "source_instance_id": str(item.get("source_instance_id") or ""),
            "integration": str(item.get("mindsdb_integration") or ""),
            "engine": str(item.get("engine") or ""),
            "source_name": str(item.get("source_name") or "") or None,
        }
        for item in sources
    ]
