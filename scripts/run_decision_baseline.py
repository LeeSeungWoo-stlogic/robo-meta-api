from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import asyncpg
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import runtime_config
from app.runtime_config import load_runtime
from app.services.decision_postgres import decide
from app.services.embedding_provider import (
    LexicalHashEmbeddingProvider,
    set_embedding_provider,
)
from app.services.metadata_repository import PostgresMetadataRepository

async def run(settings: Path, fixture_path: Path, metadata_host: str) -> dict:
    fixture = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    runtime = load_runtime(settings)
    selected_provider = os.environ.get(
        "DECISION_BASELINE_PROVIDER",
        "lexical_test",
    ).lower()
    runtime = replace(
        runtime,
        metadata=replace(runtime.metadata, host=metadata_host),
    )
    if selected_provider == "lexical_test":
        runtime = replace(
            runtime,
            embedding=replace(
                runtime.embedding,
                model="lexical-hash-test",
                dimensions=1024,
                auth_mode="none",
                api_key=None,
            ),
            decision=replace(runtime.decision, minimum_similarity=0.000001),
        )
    elif selected_provider != "openai":
        raise ValueError(
            f"unsupported DECISION_BASELINE_PROVIDER: {selected_provider}"
        )
    runtime_config._runtime = runtime
    set_embedding_provider(
        LexicalHashEmbeddingProvider()
        if selected_provider == "lexical_test"
        else None
    )
    pool = await asyncpg.create_pool(runtime.metadata.dsn, min_size=1, max_size=2)
    repository = PostgresMetadataRepository(pool, runtime)
    search_results = []
    join_results = []
    entity_results = []
    try:
        for case in fixture["search_cases"]:
            response = await decide(
                repository,
                query=case["question"],
                include_matched_columns=True,
                column_top_m=10,
                auto_resolve_entities=True,
            )
            names = [candidate.table_name for candidate in response.candidates[:3]]
            search_results.append(
                {
                    **case,
                    "actual_top3": names,
                    "passed": case["expected_table"] in names
                    and response.execution_context is not None
                    and response.execution_context.source_instance_id
                    == fixture["source_instance_id"],
                }
            )
        for case in fixture["join_cases"]:
            response = await decide(
                repository,
                query=case["question"],
                include_matched_columns=True,
                column_top_m=10,
                auto_resolve_entities=True,
            )
            bridges = [
                {"from": bridge.from_, "to": bridge.to}
                for group in response.join_groups
                for bridge in group.bridges
            ]
            join_results.append(
                {
                    **case,
                    "actual_bridges": bridges,
                    "passed": any(
                        item["from"] == case["expected_from"]
                        and item["to"] == case["expected_to"]
                        for item in bridges
                    ),
                }
            )
        for case in fixture["entity_negative_cases"]:
            response = await decide(
                repository,
                query=case["question"],
                include_matched_columns=True,
                column_top_m=10,
                auto_resolve_entities=True,
            )
            count = len(response.resolved_entities)
            entity_results.append(
                {
                    **case,
                    "actual_resolved_count": count,
                    "passed": count == case["expected_resolved_count"],
                }
            )
    finally:
        await pool.close()
        set_embedding_provider(None)
    all_results = search_results + join_results + entity_results
    return {
        "contract_version": fixture["contract_version"],
        "provider": (
            "lexical-hash-test"
            if selected_provider == "lexical_test"
            else runtime.embedding.model
        ),
        "dimensions": 1024,
        "search": search_results,
        "join": join_results,
        "entity_negative": entity_results,
        "passed": sum(bool(item["passed"]) for item in all_results),
        "total": len(all_results),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--settings",
        type=Path,
        default=ROOT / "config" / "runtime-settings.docker.local.yaml",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=ROOT / "tests" / "fixtures" / "decision" / "rwis-functional.yaml",
    )
    parser.add_argument("--metadata-host", default="127.0.0.1")
    args = parser.parse_args()
    report = asyncio.run(run(args.settings, args.fixture, args.metadata_host))
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "decision-baseline-functional.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"passed={report['passed']}/{report['total']}")
    if report["passed"] != report["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
