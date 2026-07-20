from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import yaml


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _normalized_bridge(item: dict[str, Any]) -> frozenset[str]:
    return frozenset({str(item["from"]), str(item["to"])})


def _load_fixture(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    overlay = yaml.safe_load(path.read_text(encoding="utf-8"))
    hashes = {
        "fixture_sha256": __import__("hashlib").sha256(
            path.read_bytes()
        ).hexdigest()
    }
    if "base_fixture" not in overlay:
        return overlay, hashes
    base_path = (path.parent / overlay["base_fixture"]).resolve()
    fixture = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    hashes["base_fixture_sha256"] = __import__("hashlib").sha256(
        base_path.read_bytes()
    ).hexdigest()
    for correction in overlay.get("corrections") or []:
        target = next(
            (
                item
                for item in fixture["search_cases"]
                if item["id"] == correction["id"]
            ),
            None,
        )
        if target is None:
            raise ValueError(f"fixture correction target missing: {correction['id']}")
        target["question"] = correction["question"]
        target["correction_reason"] = correction["reason"]
    fixture["contract_version"] = overlay["contract_version"]
    fixture["fixture_revision"] = overlay.get("fixture_revision")
    return fixture, hashes


async def _decision(
    client: httpx.AsyncClient,
    api_url: str,
    question: str,
) -> dict[str, Any]:
    response = await client.post(
        api_url.rstrip("/") + "/v1/data_decision",
        json={
            "query": question,
            "include_matched_columns": True,
            "column_top_m": 5,
            "table_limit": 8,
            "auto_resolve_entities": True,
        },
    )
    response.raise_for_status()
    return response.json()


async def run(
    fixture_path: Path,
    *,
    api_url: str,
    metadata_dsn: str,
) -> dict[str, Any]:
    fixture, fixture_hashes = _load_fixture(fixture_path)
    search_details: list[dict[str, Any]] = []
    join_details: list[dict[str, Any]] = []
    positive_details: list[dict[str, Any]] = []
    negative_details: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=90) as client:
        for case in fixture["search_cases"]:
            body = await _decision(client, api_url, case["question"])
            names = [
                str(item["table_name"]) for item in body["candidates"][:3]
            ]
            expected = set(case["expected_tables"])
            rank = next(
                (
                    index
                    for index, name in enumerate(names, 1)
                    if name in expected
                ),
                None,
            )
            expected_columns = set(case.get("expected_columns") or [])
            actual_columns = {
                str(column["column_name"])
                for candidate in body["candidates"]
                if candidate["table_name"] in expected
                for column in candidate.get("matched_columns") or []
            }
            search_details.append(
                {
                    **case,
                    "actual_top3": names,
                    "rank": rank,
                    "actual_columns_for_expected_table": sorted(actual_columns),
                    "column_hit": (
                        bool(expected_columns & actual_columns)
                        if expected_columns
                        else None
                    ),
                    "source_instance_id": (
                        body.get("execution_context") or {}
                    ).get("source_instance_id"),
                }
            )
        for case in fixture["join_cases"]:
            body = await _decision(client, api_url, case["question"])
            bridges = [
                bridge
                for group in body.get("join_groups") or []
                for bridge in group.get("bridges") or []
            ]
            expected = frozenset(
                {case["expected_from"], case["expected_to"]}
            )
            join_details.append(
                {
                    **case,
                    "actual_bridges": bridges,
                    "matched": any(
                        _normalized_bridge(item) == expected for item in bridges
                    ),
                }
            )
        for case in fixture["entity_positive_cases"]:
            body = await _decision(client, api_url, case["question"])
            entities = body.get("resolved_entities") or []
            matched = any(
                entity.get("mention") == case["mention"]
                and entity.get("table") == case["expected_table"]
                and entity.get("name_column") == case["expected_column"]
                and any(
                    value.get("code") == case["expected_code"]
                    for value in entity.get("values") or []
                )
                for entity in entities
            )
            positive_details.append(
                {**case, "actual_entities": entities, "matched": matched}
            )
        for case in fixture["entity_negative_cases"]:
            body = await _decision(client, api_url, case["question"])
            entities = body.get("resolved_entities") or []
            negative_details.append(
                {
                    **case,
                    "actual_entities": entities,
                    "false_positive": bool(entities),
                }
            )

    recall_at_1 = _mean(
        [1.0 if item["rank"] == 1 else 0.0 for item in search_details]
    )
    recall_at_3 = _mean(
        [1.0 if item["rank"] is not None else 0.0 for item in search_details]
    )
    precision_at_1 = recall_at_1
    precision_at_3 = _mean(
        [
            len(set(item["actual_top3"]) & set(item["expected_tables"])) / 3.0
            for item in search_details
        ]
    )
    mrr = _mean(
        [1.0 / item["rank"] if item["rank"] else 0.0 for item in search_details]
    )
    column_cases = [
        item for item in search_details if item["column_hit"] is not None
    ]
    column_recall = _mean(
        [1.0 if item["column_hit"] else 0.0 for item in column_cases]
    )
    join_recall = _mean(
        [1.0 if item["matched"] else 0.0 for item in join_details]
    )
    expected_pairs = {
        frozenset({item["expected_from"], item["expected_to"]})
        for item in join_details
    }
    observed_pairs = {
        _normalized_bridge(bridge)
        for item in join_details
        for bridge in item["actual_bridges"]
    }
    join_precision = (
        len(expected_pairs & observed_pairs) / len(observed_pairs)
        if observed_pairs
        else 0.0
    )
    entity_exact = _mean(
        [1.0 if item["matched"] else 0.0 for item in positive_details]
    )
    negative_false_positive = _mean(
        [1.0 if item["false_positive"] else 0.0 for item in negative_details]
    )
    connection = await asyncpg.connect(metadata_dsn)
    try:
        cross_source = int(
            await connection.fetchval(
                """
                SELECT count(*)
                FROM t2s_fk_constraints fk
                JOIN t2s_columns cf ON cf.id=fk.from_column_id
                JOIN t2s_tables tf ON tf.id=cf.table_id
                JOIN t2s_columns ct ON ct.id=fk.to_column_id
                JOIN t2s_tables tt ON tt.id=ct.table_id
                WHERE tf.datasource_id<>tt.datasource_id
                """
            )
        )
    finally:
        await connection.close()
    metrics = {
        "table_recall_at_1": recall_at_1,
        "table_recall_at_3": recall_at_3,
        "table_precision_at_1": precision_at_1,
        "table_precision_at_3_macro": precision_at_3,
        "mrr": mrr,
        "zero_result_rate": _mean(
            [1.0 if not item["actual_top3"] else 0.0 for item in search_details]
        ),
        "column_recall_at_5": column_recall,
        "approved_join_precision": join_precision,
        "approved_join_recall": join_recall,
        "entity_positive_exact_match": entity_exact,
        "entity_negative_false_positive": negative_false_positive,
        "cross_source_bridges": cross_source,
    }
    gates = {
        "table_recall_at_3": recall_at_3 >= 0.90,
        "table_precision_at_1": precision_at_1 >= 0.80,
        "mrr": mrr >= 0.80,
        "column_recall_at_5": column_recall >= 0.80,
        "approved_join_precision": join_precision == 1.0,
        "approved_join_recall": join_recall >= 0.75,
        "entity_positive_exact_match": entity_exact >= 0.90,
        "entity_negative_false_positive": negative_false_positive == 0.0,
        "cross_source_bridges": cross_source == 0,
    }
    return {
        "contract_version": fixture["contract_version"],
        **fixture_hashes,
        "fixture_revision": fixture.get("fixture_revision"),
        "endpoint": "/v1/data_decision",
        "counts": {
            "search": len(search_details),
            "columns": len(column_cases),
            "joins": len(join_details),
            "entity_positive": len(positive_details),
            "entity_negative": len(negative_details),
        },
        "metrics": metrics,
        "gates": gates,
        "details": {
            "search": search_details,
            "joins": join_details,
            "entity_positive": positive_details,
            "entity_negative": negative_details,
        },
        "passed": all(gates.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-url", default="http://127.0.0.1:18100")
    parser.add_argument("--metadata-dsn", required=True)
    args = parser.parse_args()
    report = asyncio.run(
        run(
            args.fixture,
            api_url=args.api_url,
            metadata_dsn=args.metadata_dsn,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report["metrics"], ensure_ascii=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
