from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import runtime_config  # noqa: E402
from app.runtime_config import load_runtime  # noqa: E402
from app.services.query_runner_mindsdb import execute  # noqa: E402
from app.services.sql_guard import GuardError  # noqa: E402


async def run(
    settings: Path,
    fixture_path: Path,
    mindsdb_url: str,
) -> dict:
    fixture = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    runtime = load_runtime(settings)
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    runtime = replace(
        runtime,
        execution=replace(
            runtime.execution,
            sql_api_url=mindsdb_url,
            audit_log_path=str(reports / "query-execute-audit-functional.jsonl"),
        ),
    )
    runtime_config._runtime = runtime
    context = fixture["execution_context"]
    accepted = []
    rejected = []
    for case in fixture["accepted"]:
        case_context = case.get("execution_context") or context
        result = await execute(
            case["sql"],
            timeout_s=10,
            max_rows=1,
            caller="functional-regression",
            execution_context=case_context,
        )
        accepted.append(
            {
                **case,
                "actual_status": result["status"],
                "row_count": result["row_count"],
                "query_id_present": bool(result["query_id"]),
                "passed": result["status"] == case["expected_status"]
                and result["datasource"] == case_context["source_instance_id"]
                and bool(result["query_id"]),
            }
        )
    for case in fixture["rejected"]:
        blocked = False
        try:
            await execute(
                case["sql"],
                timeout_s=5,
                max_rows=1,
                caller="functional-regression",
                execution_context=context,
            )
        except GuardError:
            blocked = True
        rejected.append({**case, "passed": blocked})
    results = accepted + rejected
    return {
        "contract_version": fixture["contract_version"],
        "accepted": accepted,
        "rejected": rejected,
        "passed": sum(bool(item["passed"]) for item in results),
        "total": len(results),
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
        default=(
            ROOT
            / "tests"
            / "fixtures"
            / "query_execute"
            / "rwis-functional.yaml"
        ),
    )
    parser.add_argument(
        "--mindsdb-url",
        default="http://127.0.0.1:47334/api/sql/query",
    )
    args = parser.parse_args()
    report = asyncio.run(run(args.settings, args.fixture, args.mindsdb_url))
    (ROOT / "reports" / "query-execute-baseline-functional.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"passed={report['passed']}/{report['total']}")
    if report["passed"] != report["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
