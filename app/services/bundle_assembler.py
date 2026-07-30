"""Metadata Context Bundle v2 조립 (플랜 5C).

선택된 published Artifact와 Snapshot·glossary에서 `/semantic_decision` 응답을
만든다. v1 `DecisionResponse`와 완전히 독립된 meta_version="2" 계약이다.
적합 Artifact가 없으면 readiness=blocked와 blocker를 반환한다 (즉석 생성 금지).
"""
from __future__ import annotations

from typing import Any

from .artifact_selector import SelectionResult

BUNDLE_META_VERSION = "2"


def _schema_context_from_snapshot(
    snapshot: dict[str, Any], allowed_tables: set[str]
) -> dict[str, Any]:
    tables, columns = [], []
    logical_database = snapshot.get("logical_database", "")
    for obj in snapshot.get("objects", []):
        if obj["object_name"] not in allowed_tables:
            continue
        pk = next((c["columns"] for c in obj.get("constraints", [])
                   if c.get("constraint_type") == "PK"), [])
        tables.append({
            "logical_database": logical_database,
            "schema_name": obj["schema_name"],
            "table_name": obj["object_name"],
            "comment": obj.get("comment"),
            "pk_columns": list(pk),
        })
        for column in obj.get("columns", []):
            columns.append({
                "logical_database": logical_database,
                "schema_name": obj["schema_name"],
                "table_name": obj["object_name"],
                "column_name": column["column_name"],
                "data_type": column["data_type"],
                "is_nullable": column.get("is_nullable", True),
                "comment": column.get("comment"),
            })
    return {"tables": tables, "columns": columns}


def _relationships_from_artifacts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    relationships: list[dict[str, Any]] = []
    for record in records:
        for binding in (record.get("payload") or {}).get("bindings", []):
            for join in (binding.get("spec") or {}).get("join_conditions") or []:
                left = join["left"]
                right = join["right"]
                left_fqn = (f"{left['logical_database']}.{left['schema_name']}."
                            f"{left['object_name']}.{left['column_name']}")
                right_fqn = (f"{right['logical_database']}.{right['schema_name']}."
                             f"{right['object_name']}.{right['column_name']}")
                key = (left_fqn, right_fqn)
                if key in seen:
                    continue
                seen.add(key)
                relationships.append({
                    "kind": "logical_join",
                    "left": left_fqn,
                    "right": right_fqn,
                    "join_type": (binding.get("spec") or {}).get("join_type") or "INNER",
                })
    return relationships


def _metric_catalog(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        for binding in (record.get("payload") or {}).get("bindings", []):
            if binding.get("binding_type") != "METRIC" or binding["name"] in seen:
                continue
            seen.add(binding["name"])
            spec = binding.get("spec") or {}
            catalog.append({
                "name": binding["name"],
                "concept_iri": (binding.get("concept") or {}).get("iri"),
                "expression_sql": spec.get("expression_sql") or "",
                "aggregation": spec.get("aggregation") or "NONE",
                "unit": spec.get("unit"),
                "grain": spec.get("grain") or "",
            })
    return catalog


def _matched_terms(question: str, glossary: dict[str, Any]) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for term in glossary.get("terms", []):
        surface = term.get("standard_term") or ""
        if surface and surface in question:
            matched.append({"mention": surface, "standard_term": surface,
                            "match_kind": "exact", "confidence": 1.0})
    for synonym in glossary.get("synonyms", []):
        surface = synonym.get("synonym") or ""
        if surface and surface in question and surface not in {
                m["mention"] for m in matched}:
            matched.append({"mention": surface,
                            "standard_term": synonym.get("standard_term") or surface,
                            "match_kind": "synonym", "confidence": 0.95})
    return matched


def assemble_bundle(
    *,
    question: str,
    selection: SelectionResult,
    snapshots: dict[str, dict[str, Any]],
    glossary: dict[str, Any],
    execution_context: dict[str, Any] | None,
    profiles: list[dict[str, Any]] | None = None,
    code_values: list[dict[str, Any]] | None = None,
    few_shots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not selection.ready:
        return {
            "meta_version": BUNDLE_META_VERSION,
            "query_matches": {"matched_terms": _matched_terms(question, glossary),
                              "ambiguities": []},
            "glossary": {"terms": glossary.get("terms", []),
                         "synonyms": glossary.get("synonyms", [])},
            "schema_context": {"tables": [], "columns": [], "relationships": [],
                               "code_values": [], "profiles": []},
            "few_shots": [],
            "semantic_views": [],
            "metric_catalog": [],
            "evidence": {},
            "readiness": {"state": "blocked", "blockers": selection.blockers},
            "execution_context": execution_context,
        }

    records = [s.record for s in selection.selected]
    allowed_tables: set[str] = set()
    for record in records:
        for binding in (record.get("payload") or {}).get("bindings", []):
            spec = binding.get("spec") or {}
            if spec.get("base_object"):
                allowed_tables.add(spec["base_object"]["object_name"])
            for ref in spec.get("source_columns") or []:
                allowed_tables.add(ref["object_name"])
            for join in spec.get("join_conditions") or []:
                allowed_tables.add(join["left"]["object_name"])
                allowed_tables.add(join["right"]["object_name"])

    tables: list[dict[str, Any]] = []
    columns: list[dict[str, Any]] = []
    for snapshot_id in sorted({r["snapshot_id"] for r in records}):
        snapshot = snapshots.get(snapshot_id)
        if snapshot is None:
            continue
        context = _schema_context_from_snapshot(snapshot, allowed_tables)
        tables.extend(context["tables"])
        columns.extend(context["columns"])

    return {
        "meta_version": BUNDLE_META_VERSION,
        "query_matches": {"matched_terms": _matched_terms(question, glossary),
                          "ambiguities": []},
        "glossary": {"terms": glossary.get("terms", []),
                     "synonyms": glossary.get("synonyms", [])},
        "schema_context": {
            "tables": tables,
            "columns": columns,
            "relationships": _relationships_from_artifacts(records),
            "code_values": code_values or [],
            "profiles": profiles or [],
        },
        "few_shots": few_shots or [],
        "semantic_views": [record["payload"] for record in records],
        "metric_catalog": _metric_catalog(records),
        "evidence": {
            "artifacts": [
                {"artifact_id": s.record["artifact_id"],
                 "payload_sha256": s.record["payload_sha256"],
                 "snapshot_id": s.record["snapshot_id"],
                 "score": s.score,
                 "term_hits": s.term_hits,
                 "embedding_similarity": s.embedding_similarity}
                for s in selection.selected
            ],
        },
        "readiness": {"state": "ready", "blockers": []},
        "execution_context": execution_context,
    }
