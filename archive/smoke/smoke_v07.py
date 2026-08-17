"""v0.7 RC body 회귀 — v0.6 8 endpoint + resolution 필드."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# smoke_v06 재사용
sys.path.insert(0, str(Path(__file__).parent))
import smoke_v06  # noqa: E402

BASE = os.getenv("ROBO_META_API_BASE", "http://127.0.0.1:8100")


def main() -> int:
    os.environ["ROBO_META_API_BASE"] = BASE
    smoke_v06.BASE = BASE
    code = smoke_v06.main()
    if code != 0:
        return code

    import requests

    r = requests.post(
        f"{BASE}/data_decision",
        json={"query": "수지정수장 계측값 현황", "auto_resolve_entities": True},
        timeout=60,
    )
    if r.status_code != 200:
        print(f"v0.7 data_decision failed: {r.status_code} {r.text[:200]}")
        return 1
    body = r.json()
    if body.get("meta_version") != "1.0":
        print(f"expected meta_version 1.0 got {body.get('meta_version')}")
        return 1
    for key in ("resolved_entities", "suggested_probes", "resolution_status"):
        if key not in body:
            print(f"missing v0.7 key: {key}")
            return 1
    print(f"[v0.7] resolution_status={body.get('resolution_status')} entities={len(body.get('resolved_entities') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
