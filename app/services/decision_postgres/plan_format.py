from __future__ import annotations

from collections import defaultdict
from typing import Any

from ...schemas import (
    DecisionCandidate,
    JoinBridge,
    JoinGroup,
    PlannedFilter,
    PlannedJoinCondition,
    PlannedJoinPath,
    TableKey,
)
from ..decision_planner import CompositeJoinEdge


def _table_key(table: dict[str, Any]) -> TableKey:
    return TableKey(
        db=str(table.get("source_name") or table.get("db") or "") or None,
        schema_name=str(table.get("schema_name") or ""),
        table_name=str(table.get("original_name") or table.get("name") or ""),
    )


def _path_table_ids(path: list[CompositeJoinEdge]) -> set[int]:
    ids: set[int] = set()
    for edge in path:
        ids.update({edge.left_table_id, edge.right_table_id})
    return ids


def _planned_paths(
    paths: list[list[CompositeJoinEdge]],
    tables_by_id: dict[int, dict[str, Any]],
) -> list[PlannedJoinPath]:
    planned: list[PlannedJoinPath] = []
    for path in paths:
        if not path:
            continue
        path_ids = _path_table_ids(path)
        degree: dict[int, int] = defaultdict(int)
        for edge in path:
            degree[edge.left_table_id] += 1
            degree[edge.right_table_id] += 1
        endpoints = sorted(
            (table_id for table_id, value in degree.items() if value == 1)
        )
        if len(endpoints) < 2:
            endpoints = sorted(path_ids)[:2]
        if len(endpoints) < 2:
            continue
        from_id, to_id = endpoints[0], endpoints[-1]
        bridge_ids = path_ids - {from_id, to_id}
        conditions = [
            PlannedJoinCondition(
                **{
                    "from": condition.from_fqn,
                    "to": condition.to_fqn,
                    "origin": condition.origin,
                    "confidence": condition.confidence,
                }
            )
            for edge in path
            for condition in edge.conditions
        ]
        planned.append(
            PlannedJoinPath(
                from_table=_table_key(tables_by_id[from_id]),
                to_table=_table_key(tables_by_id[to_id]),
                hop_count=len(path),
                conditions=conditions,
                bridge_tables=[
                    _table_key(tables_by_id[table_id])
                    for table_id in sorted(bridge_ids)
                    if table_id in tables_by_id
                ],
                confidence=min(
                    (edge.confidence for edge in path),
                    default=0.0,
                ),
            )
        )
    return planned


def _strategy(
    paths: list[PlannedJoinPath],
    filters: list[PlannedFilter],
) -> str | None:
    if not paths:
        if any(item.operator in {"EQ", "IN"} for item in filters):
            return "exists_filter"
        return None
    max_hops = max(path.hop_count for path in paths)
    if max_hops >= 3:
        return "cte_then_join"
    if max_hops == 2:
        return "derived_join"
    return "simple_join"


def _top_target(candidates: list[DecisionCandidate]) -> str:
    if not candidates:
        return "none"
    classes = {candidate.target_class for candidate in candidates}
    if "analytic" in classes:
        return "analytic"
    if "collect" in classes and "source" not in classes:
        return "collect"
    return "source"


def assemble_join_groups(
    *,
    planned_paths: list[PlannedJoinPath],
    tables_by_id: dict[int, dict[str, Any]],
    selected_table_ids: set[int],
    bridge_table_ids: set[int],
    bridge_tables: list[TableKey],
    strategy: str | None,
    required_roles: list[str],
) -> list[JoinGroup]:
    bridges = [
        JoinBridge(
            **{
                "from": condition.from_,
                "to": condition.to,
                "via": "fk",
                "confidence": condition.confidence,
            }
        )
        for path in planned_paths
        for condition in path.conditions
    ]
    if not bridges:
        return []
    return [
        JoinGroup(
            members=[
                _table_key(tables_by_id[table_id])
                for table_id in sorted(selected_table_ids)
                if table_id in tables_by_id
                and table_id not in bridge_table_ids
            ],
            bridge_tables=bridge_tables,
            recommended_strategy=strategy,
            bridges=bridges,
            group_score=min(
                (path.confidence for path in planned_paths),
                default=0.0,
            ),
            score_breakdown={
                "hop_count": sum(path.hop_count for path in planned_paths),
                "required_roles": len(required_roles),
            },
            rationale="HyDE 역할 seed를 승인 JOIN 최단 경로로 연결",
        )
    ]
