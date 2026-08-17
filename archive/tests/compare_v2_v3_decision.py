"""v2 vs v3 /data_decision plan query 비교."""
import json
import sys
from pathlib import Path

import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

QUERY = "유역본부 내 정수장별 공급량은?"
OUT = Path(__file__).parent.parent / "logs" / "data_decision_test" / "v2_v3_compare.json"

rows = []
for label, port in [("v2", 8098), ("v3", 8099)]:
    r = requests.post(
        f"http://127.0.0.1:{port}/data_decision",
        json={"query": QUERY, "include_matched_columns": True},
        timeout=120,
    )
    d = r.json()
    jgs = d.get("join_groups") or []
    rows.append({
        "version": label,
        "port": port,
        "target": d.get("target"),
        "confidence": d.get("confidence"),
        "join_groups_mode": (d.get("threshold_used") or {}).get("join_groups_mode"),
        "candidates": [
            {"schema": c.get("schema_name"), "table": c.get("table_name"), "score": c.get("score")}
            for c in (d.get("candidates") or [])
        ],
        "join_groups": jgs,
    })

OUT.write_text(json.dumps({"query": QUERY, "results": rows}, ensure_ascii=False, indent=2), encoding="utf-8")

for row in rows:
    print(f"=== {row['version']} (:{row['port']}) ===")
    print(f"target={row['target']} join_groups_mode={row['join_groups_mode']}")
    print(f"candidates={len(row['candidates'])} join_groups={len(row['join_groups'])}")
    for i, jg in enumerate(row["join_groups"]):
        mem = [f"{m.get('schema_name')}.{m.get('table_name')}" for m in jg.get("members") or []]
        print(f"  group[{i}] {mem}")
        for b in (jg.get("bridges") or [])[:2]:
            print(f"    {b.get('from')} -[{b.get('via')}]-> {b.get('to')} conf={b.get('confidence')}")
            print(f"      path={b.get('path')}")
    if not row["join_groups"]:
        print("  (empty join_groups)")
    print()
print(f"Saved: {OUT}")
