from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

TABLE_CASES = [
    ("시간 단위 계측값", "RDF01HH_TB", "tagsn"),
    ("일 단위 계측값", "RDD01DD_TB", "tagsn"),
    ("원격 감시 정보", "RDIRSINFO_TB", "sys_code"),
    ("사무소 코드 정보", "RDISAMU_TB", "sms_code"),
    ("사업장 정보", "RDISAUP_TB", "suj_code"),
]


async def run(api_url: str) -> dict[str, Any]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=90) as client:
        contexts: dict[str, dict[str, Any]] = {}
        for question, table, column in TABLE_CASES:
            decision = await client.post(
                api_url.rstrip("/") + "/v1/data_decision",
                json={
                    "query": question,
                    "include_matched_columns": True,
                    "table_limit": 8,
                },
            )
            decision.raise_for_status()
            context = decision.json()["execution_context"]
            contexts[table] = context
            sql = (
                f"SELECT {column} FROM "
                f"rwis_postgres_active.{table} LIMIT 1"
            )
            response = await client.post(
                api_url.rstrip("/") + "/v1/query_execute",
                json={
                    "sql": sql,
                    "execution_context": context,
                    "timeout_s": 10,
                    "max_rows": 1,
                },
            )
            response.raise_for_status()
            body = response.json()
            accepted.append(
                {
                    "table": table,
                    "sql": sql,
                    "status": body["status"],
                    "columns": body["columns"],
                    "row_count": body["row_count"],
                    "query_id_present": bool(body.get("query_id")),
                    "datasource": body.get("datasource"),
                    "timeout_s_applied": body["timeout_s_applied"],
                    "max_rows_applied": body["max_rows_applied"],
                    "passed": (
                        body["status"] == "ok"
                        and body["row_count"] <= 1
                        and bool(body["columns"])
                        and bool(body.get("query_id"))
                        and body.get("datasource") == "rwis-pg"
                        and body["timeout_s_applied"] == 10
                        and body["max_rows_applied"] == 1
                    ),
                }
            )
        context = contexts["RDF01HH_TB"]
        blocked_cases = [
            (
                "other_catalog",
                "SELECT * FROM other.RDF01HH_TB",
            ),
            (
                "other_table",
                "SELECT * FROM rwis_postgres_active.OTHER_TABLE",
            ),
            (
                "unqualified",
                "SELECT * FROM RDF01HH_TB",
            ),
            (
                "write_statement",
                "DELETE FROM rwis_postgres_active.RDF01HH_TB",
            ),
        ]
        for case_id, sql in blocked_cases:
            response = await client.post(
                api_url.rstrip("/") + "/v1/query_execute",
                json={"sql": sql, "execution_context": context},
            )
            rejected.append(
                {
                    "id": case_id,
                    "sql": sql,
                    "http_status": response.status_code,
                    "passed": response.status_code >= 400,
                }
            )
    results = accepted + rejected
    return {
        "endpoint": "/v1/query_execute",
        "accepted": accepted,
        "rejected": rejected,
        "counts": {
            "accepted": len(accepted),
            "rejected": len(rejected),
        },
        "passed_cases": sum(bool(item["passed"]) for item in results),
        "total_cases": len(results),
        "passed": all(bool(item["passed"]) for item in results),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:18100")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run(args.api_url))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"passed={report['passed_cases']}/{report['total_cases']} "
        f"accepted={report['counts']['accepted']}"
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
