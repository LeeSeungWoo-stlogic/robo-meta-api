"""Deterministic candidate merge and approved-join graph planning."""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any


def _table_fqn(table: dict[str, Any]) -> str:
    return (
        f"{table.get('schema_name') or ''}."
        f"{table.get('original_name') or table.get('name') or ''}"
    ).strip(".")


def merge_axis_candidates(
    axis_results: dict[str, list[dict[str, Any]]],
    *,
    question_weight: float,
    hyde_weight: float,
    role_weight: float,
    limit: int,
) -> list[dict[str, Any]]:
    bucket: dict[int, dict[str, Any]] = {}
    has_hyde_axis = bool(axis_results.get("hyde"))
    for axis, rows in axis_results.items():
        for row in rows:
            table_id = int(row["id"])
            item = bucket.setdefault(
                table_id,
                {**row, "axis_scores": {}, "role_scores": {}},
            )
            score = float(row.get("score") or 0.0)
            item["axis_scores"][axis] = max(
                score,
                float(item["axis_scores"].get(axis) or 0.0),
            )
            if axis.startswith("role:"):
                role = axis.split(":", 1)[1]
                item["role_scores"][role] = max(
                    score,
                    float(item["role_scores"].get(role) or 0.0),
                )

    merged: list[dict[str, Any]] = []
    for item in bucket.values():
        axes = item["axis_scores"]
        role_score = max(item["role_scores"].values(), default=0.0)
        score = (
            (question_weight if has_hyde_axis else 1.0)
            * float(axes.get("question") or 0.0)
            + hyde_weight * float(axes.get("hyde") or 0.0)
            + role_weight * role_score
        )
        item["score"] = score
        merged.append(item)
    return sorted(
        merged,
        key=lambda item: (
            -float(item.get("score") or 0.0),
            _table_fqn(item).lower(),
        ),
    )[: max(1, limit)]


def prune_by_score_gap(
    candidates: list[dict[str, Any]],
    *,
    max_k: int,
    gap_ratio: float,
    min_step: float,
    top_radius: float,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    ordered = sorted(
        candidates,
        key=lambda item: (
            -float(item.get("score") or 0.0),
            _table_fqn(item).lower(),
        ),
    )
    top_score = float(ordered[0].get("score") or 0.0)
    if top_score <= 0:
        return ordered[:1]
    cutoff = top_score * max(0.0, min(1.0, gap_ratio))
    if top_radius > 0:
        cutoff = max(cutoff, top_score - top_radius)
    selected: list[dict[str, Any]] = []
    for item in ordered[: max(1, max_k)]:
        score = float(item.get("score") or 0.0)
        if selected and score < cutoff:
            break
        if (
            selected
            and min_step > 0
            and float(selected[-1].get("score") or 0.0) - score > min_step
        ):
            break
        selected.append(item)
    return selected


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


@dataclass(frozen=True)
class JoinConditionData:
    from_fqn: str
    to_fqn: str
    origin: str
    confidence: float


@dataclass
class CompositeJoinEdge:
    left_table_id: int
    right_table_id: int
    conditions: list[JoinConditionData] = field(default_factory=list)
    confidence: float = 0.0

    def other(self, table_id: int) -> int:
        if table_id == self.left_table_id:
            return self.right_table_id
        if table_id == self.right_table_id:
            return self.left_table_id
        raise ValueError(f"table {table_id} is not part of edge")


def build_composite_edges(
    rows: list[dict[str, Any]],
) -> list[CompositeJoinEdge]:
    grouped: dict[tuple[int, int], CompositeJoinEdge] = {}
    for row in rows:
        from_id = int(row["from_table_id"])
        to_id = int(row["to_table_id"])
        if from_id == to_id:
            continue
        key = tuple(sorted((from_id, to_id)))
        metadata = _metadata(row.get("metadata"))
        origins = metadata.get("origins") or [metadata.get("origin") or "approved"]
        if not isinstance(origins, list):
            origins = [str(origins)]
        confidence = float(metadata.get("confidence") or 1.0)
        condition = JoinConditionData(
            from_fqn=(
                f"{row['from_schema']}.{row['from_table']}."
                f"{row['from_column']}"
            ),
            to_fqn=(
                f"{row['to_schema']}.{row['to_table']}."
                f"{row['to_column']}"
            ),
            origin=str(origins[0] if origins else "approved"),
            confidence=confidence,
        )
        edge = grouped.setdefault(
            key,
            CompositeJoinEdge(
                left_table_id=key[0],
                right_table_id=key[1],
            ),
        )
        condition_key = frozenset(
            {condition.from_fqn.lower(), condition.to_fqn.lower()}
        )
        if not any(
            frozenset(
                {existing.from_fqn.lower(), existing.to_fqn.lower()}
            )
            == condition_key
            for existing in edge.conditions
        ):
            edge.conditions.append(condition)

    result: list[CompositeJoinEdge] = []
    for edge in grouped.values():
        edge.conditions.sort(
            key=lambda condition: (
                condition.from_fqn.lower(),
                condition.to_fqn.lower(),
            )
        )
        edge.confidence = min(
            (condition.confidence for condition in edge.conditions),
            default=0.0,
        )
        result.append(edge)
    return sorted(
        result,
        key=lambda edge: (edge.left_table_id, edge.right_table_id),
    )


def shortest_path(
    edges: list[CompositeJoinEdge],
    *,
    source_ids: set[int],
    target_id: int,
    max_hops: int,
) -> list[CompositeJoinEdge] | None:
    if target_id in source_ids:
        return []
    adjacency: dict[int, list[CompositeJoinEdge]] = {}
    for edge in edges:
        adjacency.setdefault(edge.left_table_id, []).append(edge)
        adjacency.setdefault(edge.right_table_id, []).append(edge)
    for values in adjacency.values():
        values.sort(
            key=lambda edge: (
                -edge.confidence,
                edge.left_table_id,
                edge.right_table_id,
            )
        )

    queue: deque[tuple[int, list[CompositeJoinEdge], frozenset[int]]] = deque(
        (source_id, [], frozenset({source_id}))
        for source_id in sorted(source_ids)
    )
    while queue:
        current, path, visited = queue.popleft()
        if len(path) >= max_hops:
            continue
        for edge in adjacency.get(current, []):
            next_id = edge.other(current)
            if next_id in visited:
                continue
            next_path = [*path, edge]
            if next_id == target_id:
                return next_path
            queue.append((next_id, next_path, visited | {next_id}))
    return None


@dataclass
class PlannerSelection:
    role_tables: dict[str, dict[str, Any]]
    selected_table_ids: set[int]
    bridge_table_ids: set[int]
    paths: list[list[CompositeJoinEdge]]
    unresolved: list[str]


def select_minimal_tables(
    *,
    required_roles: list[str],
    optional_roles: list[str],
    role_candidates: dict[str, list[dict[str, Any]]],
    edges: list[CompositeJoinEdge],
    max_hops: int,
    table_limit: int,
    distinct_role_pairs: set[frozenset[str]] | None = None,
) -> PlannerSelection:
    role_tables: dict[str, dict[str, Any]] = {}
    selected_ids: set[int] = set()
    bridge_ids: set[int] = set()
    paths: list[list[CompositeJoinEdge]] = []
    unresolved: list[str] = []
    distinct_pairs = distinct_role_pairs or set()

    for role in required_roles:
        candidates = role_candidates.get(role, [])
        if not candidates:
            unresolved.append(f"필수 역할 후보 없음: {role}")
            continue
        if not selected_ids:
            selected = candidates[0]
            role_tables[role] = selected
            selected_ids.add(int(selected["id"]))
            continue

        choices: list[
            tuple[int, float, float, str, dict[str, Any], list[CompositeJoinEdge]]
        ] = []
        for candidate in candidates:
            if any(
                int(selected_table["id"]) == int(candidate["id"])
                and frozenset({role, selected_role}) in distinct_pairs
                for selected_role, selected_table in role_tables.items()
            ):
                continue
            path = shortest_path(
                edges,
                source_ids=selected_ids,
                target_id=int(candidate["id"]),
                max_hops=max_hops,
            )
            if path is None:
                continue
            path_confidence = min(
                (edge.confidence for edge in path),
                default=1.0,
            )
            choices.append(
                (
                    len(path),
                    -path_confidence,
                    -float(candidate.get("score") or 0.0),
                    _table_fqn(candidate).lower(),
                    candidate,
                    path,
                )
            )
        if not choices:
            selected = next(
                (
                    candidate
                    for candidate in candidates
                    if not any(
                        int(selected_table["id"]) == int(candidate["id"])
                        and frozenset({role, selected_role}) in distinct_pairs
                        for selected_role, selected_table in role_tables.items()
                    )
                ),
                candidates[0],
            )
            role_tables[role] = selected
            selected_ids.add(int(selected["id"]))
            unresolved.append(f"승인 JOIN 경로 없음: {role}")
            continue

        _, _, _, _, selected, selected_path = sorted(choices)[0]
        role_tables[role] = selected
        selected_ids.add(int(selected["id"]))
        paths.append(selected_path)
        for edge in selected_path:
            selected_ids.update({edge.left_table_id, edge.right_table_id})

    required_ids = {
        int(table["id"]) for table in role_tables.values()
    }
    bridge_ids = selected_ids - required_ids

    for role in optional_roles:
        candidates = role_candidates.get(role, [])
        direct_choices: list[
            tuple[float, str, dict[str, Any], list[CompositeJoinEdge]]
        ] = []
        for candidate in candidates:
            path = shortest_path(
                edges,
                source_ids=selected_ids,
                target_id=int(candidate["id"]),
                max_hops=1,
            )
            if path is not None:
                direct_choices.append(
                    (
                        -float(candidate.get("score") or 0.0),
                        _table_fqn(candidate).lower(),
                        candidate,
                        path,
                    )
                )
        if direct_choices and len(selected_ids) < table_limit:
            _, _, selected, selected_path = sorted(direct_choices)[0]
            role_tables[role] = selected
            selected_ids.add(int(selected["id"]))
            if selected_path:
                paths.append(selected_path)

    if len(selected_ids) > table_limit:
        unresolved.append(
            f"최소 테이블 집합이 table_limit={table_limit}을 초과함"
        )
    return PlannerSelection(
        role_tables=role_tables,
        selected_table_ids=selected_ids,
        bridge_table_ids=bridge_ids,
        paths=paths,
        unresolved=unresolved,
    )
