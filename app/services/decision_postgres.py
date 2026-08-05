from __future__ import annotations

from collections import defaultdict
import json
import re
from typing import Any

from ..runtime_config import get_runtime
from ..schemas import (
    DecisionCandidate,
    DecisionResponse,
    ExecutionContext,
    FilterRequirement,
    JoinBridge,
    JoinGroup,
    JoinRequirement,
    MatchedColumn,
    PlannedFilter,
    PlannedJoinCondition,
    PlannedJoinPath,
    PlannedTable,
    QueryAnalysis,
    QueryPlan,
    ResolvedEntity,
    ResolvedValue,
    SchemaRoleRequirement,
    TableKey,
)
from . import subject_area as subject_area_service
from .decision_planner import (
    CompositeJoinEdge,
    build_composite_edges,
    merge_axis_candidates,
    prune_by_score_gap,
    select_minimal_tables,
)
from .embedding_provider import get_embedding_provider
from .execution_context_resolver import (
    ExecutionBindingError,
    resolve_execution_context,
)
from .metadata_repository import PostgresMetadataRepository
from .query_analysis import (
    analysis_embedding_text,
    get_query_analyzer,
    role_embedding_text,
)


def _same_source(row: dict[str, Any], source_instance_id: str) -> bool:
    return str(row.get("source_instance_id") or "") == source_instance_id


def _provisional_source_instance_id(
    vector_tables: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
) -> str:
    """Pick one source before any cross-source expand (plan order)."""

    if vector_tables:
        return str(vector_tables[0].get("source_instance_id") or "").strip()
    for mapping in mappings:
        source_id = str(mapping.get("source_instance_id") or "").strip()
        if source_id:
            return source_id
    return ""


async def _embed_question(question: str) -> list[float]:
    """v1 경로 — EmbeddingProvider 인터페이스로 위임 (기본은 기존과 동일한 HTTP)."""
    return await get_embedding_provider().embed(question)


def _target_class(subject_area: str) -> str:
    if subject_area == "agg":
        return "analytic"
    if subject_area in {"raw", "master", "code"}:
        return "source"
    if subject_area in {"hist", "link"}:
        return "collect"
    return "unknown"


def _resolve_subject_area(table: dict[str, Any]) -> str:
    """Prefer platform-published subject_area; fall back to local YAML rules."""
    for key in ("subject_area_override", "subject_area"):
        value = str(table.get(key) or "").strip().casefold()
        if value:
            return value
    return subject_area_service.classify(
        str(table.get("schema_name") or ""),
        str(table.get("original_name") or table.get("name") or ""),
    )


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _value_examples(metadata: dict[str, Any]) -> list[str]:
    sample_values = metadata.get("sample_values")
    if not isinstance(sample_values, list):
        return []
    examples: list[str] = []
    for item in sample_values:
        value = item.get("value") if isinstance(item, dict) else item
        if value is None or isinstance(value, (dict, list)):
            continue
        examples.append(str(value))
        if len(examples) == 5:
            break
    return examples


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _merge_column_hits(
    question_hits: dict[int, list[dict[str, Any]]],
    metric_hits: dict[int, list[dict[str, Any]]],
) -> dict[int, list[dict[str, Any]]]:
    merged: dict[int, list[dict[str, Any]]] = {}
    for table_id in question_hits.keys() | metric_hits.keys():
        by_column_id: dict[int, dict[str, Any]] = {}
        for column in [
            *question_hits.get(table_id, []),
            *metric_hits.get(table_id, []),
        ]:
            column_id = int(column["id"])
            current = by_column_id.get(column_id)
            if current is None or float(column.get("score") or 0.0) > float(
                current.get("score") or 0.0
            ):
                by_column_id[column_id] = column
        merged[table_id] = sorted(
            by_column_id.values(),
            key=lambda item: float(item.get("score") or 0.0),
            reverse=True,
        )
    return merged


def _candidate(
    table: dict[str, Any],
    columns: list[dict[str, Any]],
    *,
    source: str,
) -> DecisionCandidate:
    subject_area = _resolve_subject_area(table)
    matched: list[MatchedColumn] = []
    for column in columns:
        metadata = _metadata_dict(column.get("metadata"))
        constraints = []
        if column.get("is_primary_key"):
            constraints.append("PK")
        if column.get("is_foreign_key"):
            constraints.append("FK")
        matched.append(
            MatchedColumn(
                column_name=str(column["name"]),
                score=float(column.get("score") or 0.0),
                constraints=constraints,
                column_name_kr=(column.get("description") or None),
                data_type=(column.get("dtype") or None),
                description=(
                    column.get("analyzed_description")
                    or column.get("description")
                    or None
                ),
                value_examples=_value_examples(metadata),
                format_pattern=_optional_string(metadata.get("format_pattern")),
                unit=_optional_string(metadata.get("unit")),
                facility_code=_optional_string(
                    metadata.get("facility_code") or metadata.get("facility_scope")
                ),
                system_code=_optional_string(metadata.get("system_code")),
                pk_ordinal=_optional_int(metadata.get("pk_ordinal")),
            )
        )
    return DecisionCandidate(
        db=str(table.get("source_name") or table.get("db") or ""),
        schema_name=str(table.get("schema_name") or ""),
        table_name=str(table.get("original_name") or table.get("name") or ""),
        score=float(table.get("score") or 0.0),
        source=source,
        target_class=_target_class(subject_area),
        subject_area=subject_area,
        matched_columns=matched,
        table_comment=(table.get("description") or None),
        description=(
            table.get("analyzed_description")
            or table.get("description")
            or None
        ),
    )


def _resolved_entities(mappings: list[dict[str, Any]]) -> list[ResolvedEntity]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for mapping in mappings:
        key = (
            str(mapping.get("natural_value") or ""),
            str(mapping.get("db") or ""),
            str(mapping.get("schema_name") or ""),
            str(mapping.get("table_name") or ""),
        )
        grouped[key].append(mapping)
    entities: list[ResolvedEntity] = []
    for (mention, db, schema_name, table_name), rows in grouped.items():
        column_name = str(rows[0].get("column_name") or "")
        metadata = _metadata_dict(rows[0].get("metadata"))
        label_column = _optional_string(metadata.get("label_column"))
        entities.append(
            ResolvedEntity(
                mention=mention,
                entity_type="code",
                db=db or None,
                schema_name=schema_name or None,
                table=table_name,
                name_column=label_column or column_name,
                code_column=column_name,
                values=[
                    ResolvedValue(
                        code=str(row.get("code_value") or ""),
                        confidence=1.0,
                    )
                    for row in rows
                ],
                source="value_examples",
            )
        )
    return entities


async def _decide_single_vector_legacy(
    repository: PostgresMetadataRepository,
    *,
    query: str,
    include_matched_columns: bool,
    column_top_m: int | None,
    auto_resolve_entities: bool,
    table_limit: int | None = None,
) -> DecisionResponse:
    runtime = get_runtime()
    # 요청 table_limit 하나로 검색 top-k와 최종 cap을 함께 제어 (v0.7 계약,
    # decision_service._resolve_decision_policy와 동일 의미)
    effective_top_k = (
        max(1, min(50, int(table_limit)))
        if table_limit is not None
        else runtime.decision.table_top_k
    )
    embedding = await _embed_question(query)
    vector_tables = await repository.search_tables(
        embedding,
        limit=effective_top_k,
    )
    vector_tables = sorted(
        vector_tables,
        key=lambda item: float(item.get("score") or 0),
        reverse=True,
    )
    mappings = await repository.find_value_mappings(query)
    selected_source_instance = _provisional_source_instance_id(
        vector_tables, mappings
    )
    mappings = [
        mapping
        for mapping in mappings
        if selected_source_instance
        and _same_source(mapping, selected_source_instance)
    ]
    tables = [
        table
        for table in vector_tables
        if selected_source_instance
        and _same_source(table, selected_source_instance)
    ]
    mapped_table_ids = {
        int(mapping["table_id"])
        for mapping in mappings
        if mapping.get("table_id") is not None
    }
    existing_ids = {int(table["id"]) for table in tables}
    for table in await repository.fetch_tables_by_ids(mapped_table_ids - existing_ids):
        if not selected_source_instance or not _same_source(
            table, selected_source_instance
        ):
            continue
        table["score"] = 1.0
        tables.insert(0, table)
    # FK expand only after provisional source + same-source mapping seed.
    seed_table_ids = mapped_table_ids | {int(table["id"]) for table in tables}
    neighbor_ids = await repository.fk_neighbor_table_ids(seed_table_ids)
    current_ids = {int(table["id"]) for table in tables}
    for table in await repository.fetch_tables_by_ids(neighbor_ids - current_ids):
        if not selected_source_instance or not _same_source(
            table, selected_source_instance
        ):
            continue
        table["score"] = 0.99 if mapped_table_ids else 0.0
        tables.append(table)

    deduplicated: dict[int, dict[str, Any]] = {}
    for table in tables:
        table_id = int(table["id"])
        current = deduplicated.get(table_id)
        if current is None or float(table.get("score") or 0) > float(
            current.get("score") or 0
        ):
            deduplicated[table_id] = table
    tables = sorted(
        deduplicated.values(),
        key=lambda item: float(item.get("score") or 0),
        reverse=True,
    )[:effective_top_k]

    table_ids = [int(table["id"]) for table in tables]
    columns = (
        await repository.search_columns(
            embedding,
            table_ids=table_ids,
            per_table_limit=column_top_m or runtime.decision.column_top_m,
        )
        if include_matched_columns
        else {}
    )
    # 자연어가 컬럼 업무 의미를 직접 지칭하는 경우 테이블 벡터만으로는
    # 유사한 시계열/마스터 테이블의 순서가 뒤바뀔 수 있다. 승인된 컬럼
    # 벡터의 최상위 점수를 보조 신호로 사용하되 entity exact mapping의
    # 1.0 점수는 유지한다.
    if include_matched_columns:
        for table in tables:
            table_id = int(table["id"])
            table_score = float(table.get("score") or 0.0)
            column_score = max(
                (
                    float(item.get("score") or 0.0)
                    for item in columns.get(table_id, [])
                ),
                default=table_score,
            )
            table["score"] = (
                table_score
                if table_id in mapped_table_ids
                else 0.5 * table_score + 0.5 * column_score
            )
        tables.sort(
            key=lambda item: float(item.get("score") or 0.0),
            reverse=True,
        )
    mapping_table_ids = {
        int(mapping["table_id"])
        for mapping in mappings
        if mapping.get("table_id") is not None
    }
    candidates = [
        _candidate(
            table,
            columns.get(int(table["id"]), []),
            source="name_rule"
            if int(table["id"]) in mapping_table_ids
            else "vector",
        )
        for table in tables
    ]

    fk_bridges_raw = await repository.fk_bridges(table_ids)
    bridges = [
        JoinBridge(
            **{
                "from": (
                    f"{row['from_schema']}.{row['from_table']}."
                    f"{row['from_column']}"
                ),
                "to": (
                    f"{row['to_schema']}.{row['to_table']}."
                    f"{row['to_column']}"
                ),
                "via": "fk",
                "confidence": runtime.decision.verified_join_confidence,
            }
        )
        for row in fk_bridges_raw
    ]
    convention_raw = await repository.convention_bridges(table_ids)
    existing_bridge_keys = {(bridge.from_, bridge.to) for bridge in bridges}
    for row in convention_raw:
        from_value = (
            f"{row['from_schema']}.{row['from_table']}.{row['from_column']}"
        )
        to_value = f"{row['to_schema']}.{row['to_table']}.{row['to_column']}"
        if (from_value, to_value) in existing_bridge_keys:
            continue
        bridges.append(
            JoinBridge(
                **{
                    "from": from_value,
                    "to": to_value,
                    "via": "convention",
                    "confidence": runtime.decision.convention_join_confidence,
                }
            )
        )
    join_groups = []
    if bridges:
        join_groups.append(
            JoinGroup(
                members=[
                    TableKey(
                        db=candidate.db,
                        schema_name=candidate.schema_name,
                        table_name=candidate.table_name,
                    )
                    for candidate in candidates
                ],
                recommended_strategy="simple_join",
                bridges=bridges,
                group_score=max(bridge.confidence for bridge in bridges),
                rationale="물리 FK 또는 승인된 논리 join 힌트와 동일 식별자 컬럼 연결 후보",
            )
        )

    entities = _resolved_entities(mappings) if auto_resolve_entities else []
    allowed_objects = [
        str(table.get("original_name") or table.get("name") or "")
        for table in tables
    ]
    execution_context = None
    if selected_source_instance and allowed_objects:
        try:
            resolved = await resolve_execution_context(
                repository,
                source_instance_id=selected_source_instance,
                requested_objects=allowed_objects,
            )
            if resolved.source_name:
                execution_context = ExecutionContext(**resolved.public_dict())
            else:
                execution_context = None
        except ExecutionBindingError:
            execution_context = None
    return DecisionResponse(
        target="source" if candidates else "none",
        secondary_targets=[],
        confidence=float(candidates[0].score) if candidates else 0.0,
        candidates=candidates,
        join_groups=join_groups,
        threshold_used={
            "minimum_similarity": runtime.decision.minimum_similarity,
            "table_top_k": effective_top_k,
            "table_limit": table_limit,
            "column_top_m": column_top_m or runtime.decision.column_top_m,
        },
        resolved_entities=entities,
        suggested_probes=[],
        resolution_status="complete" if entities else "partial",
        execution_context=execution_context,
    )


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


def _filter_column_score(
    requirement: FilterRequirement,
    column: dict[str, Any],
) -> float:
    base = float(column.get("score") or 0.0)
    meaning = requirement.meaning.lower()
    name = str(column.get("name") or "").upper()
    descriptions = " ".join(
        str(column.get(key) or "").lower()
        for key in ("description", "analyzed_description")
    )
    boosts = (
        (("오류", "에러"), ("ERR", "ERROR")),
        (("상태",), ("STATUS", "STATE")),
        (("시간", "시각", "기간"), ("TIME", "DATE", "DT", "TM")),
        (("이름", "명칭"), ("NAME", "NM", "DESC")),
        (("코드",), ("CODE", "CD")),
        (("값", "측정"), ("VALUE", "VAL")),
    )
    lexical = 0.0
    for korean_terms, physical_terms in boosts:
        if any(term in meaning for term in korean_terms) and any(
            term in name for term in physical_terms
        ):
            lexical = max(lexical, 0.45)
    tokens = {
        token
        for token in re.findall(r"[가-힣]{2,}", meaning)
        if token not in {"있는", "항목", "조회"}
    }
    if tokens and any(token in descriptions for token in tokens):
        lexical = max(lexical, 0.25)
    return base + lexical


def _role_blob(analysis: QueryAnalysis) -> str:
    return " ".join(
        f"{role.role} {' '.join(role.search_terms)}"
        for role in analysis.schema_roles
    )


def _role_covers(role: SchemaRoleRequirement, *terms: str, exclude: tuple[str, ...] = ()) -> bool:
    text = f"{role.role} {' '.join(role.search_terms)}".casefold()
    if exclude and any(token in text for token in exclude):
        return False
    return any(term in text for term in terms)


def _enrich_analysis_roles(query: str, analysis: QueryAnalysis) -> QueryAnalysis:
    """Fill missing facility/tag/fact roles HyDE often collapses into one blob."""
    if analysis.status != "complete":
        return analysis
    q = query or ""
    roles = list(analysis.schema_roles)
    joins = list(analysis.join_requirements)
    measure_exclude = ("계측", "측정", "시계열", "팩트", "값", "현황", "데이터")

    def _covered(*terms: str, exclude: tuple[str, ...] = ()) -> bool:
        return any(_role_covers(role, *terms, exclude=exclude) for role in roles)

    def _add(role: str, terms: list[str], *, necessity: str = "required") -> None:
        if any(role == existing.role for existing in roles):
            return
        roles.append(
            SchemaRoleRequirement(
                role=role,
                necessity=necessity,  # type: ignore[arg-type]
                cardinality="many",
                search_terms=terms,
            )
        )

    plant = any(term in q for term in ("정수장", "사업장"))
    measure = any(term in q for term in ("계측", "측정", "값", "현황", "데이터"))
    region = any(term in q for term in ("충청", "금강", "한강", "낙동", "영섬", "본부", "권역", "지역"))
    inventory = any(term in q for term in ("어떤", "무엇", "항목", "데이터들"))
    timeseries = any(
        term in q for term in ("계측값", "현황", "평균", "합계", "년", "월", "일", "시간")
    ) and not inventory

    # "사업장 계측 데이터" 같은 붕괴 역할은 사업장 마스터 커버로 치지 않는다.
    if plant and not _covered("사업장", "정수장", "시설", exclude=measure_exclude):
        _add("사업장 마스터", ["사업장", "정수장", "SUJ", "사업장코드", "사업장이름", "RDISAUP"])
    if region and not _covered("본부", "권역", "유역", "지역본부", exclude=measure_exclude):
        _add("지역본부 마스터", ["본부", "지역본부", "BNB", "유역본부", "권역", "RDIBONBU"])
    if measure and plant and not _covered("태그", "측정항목", "태그마스터", exclude=("계측값", "시계열")):
        _add("태그 마스터", ["태그", "측정항목", "TAG", "TAGSN", "태그명", "RDITAG"])
    if timeseries and not _covered("일별", "월별", "시간별", "01dd", "01mm", "01hh", "팩트"):
        _add("일별 계측 팩트", ["일별", "일 단위", "계측값", "01DD", "LOG_TIME", "VAL", "RDD01DD"])

    role_names = {role.role for role in roles}
    def _link(a: str, b: str, keys: list[str]) -> None:
        if a in role_names and b in role_names:
            if any({j.from_role, j.to_role} == {a, b} for j in joins):
                return
            joins.append(
                JoinRequirement(
                    from_role=a,
                    to_role=b,
                    required=True,
                    key_meanings=keys,
                )
            )

    if "사업장 마스터" in role_names and "지역본부 마스터" in role_names:
        _link("사업장 마스터", "지역본부 마스터", ["본부코드", "BNB_CODE"])
    if "사업장 마스터" in role_names and "태그 마스터" in role_names:
        _link("사업장 마스터", "태그 마스터", ["사업장코드", "SUJ_CODE"])
    if "태그 마스터" in role_names and "일별 계측 팩트" in role_names:
        _link("태그 마스터", "일별 계측 팩트", ["태그일련번호", "TAGSN"])
    if "사업장 마스터" in role_names and "일별 계측 팩트" in role_names:
        _link("사업장 마스터", "일별 계측 팩트", ["사업장코드", "SUJ_CODE"])

    # Remove HyDE hybrid roles that collapse facility+measure into one seed.
    concrete = {"사업장 마스터", "지역본부 마스터", "태그 마스터", "일별 계측 팩트"}
    if concrete & {role.role for role in roles}:
        roles = [
            role
            for role in roles
            if role.role in concrete
            or not (
                _role_covers(role, "사업장", "정수장", "시설")
                and _role_covers(role, "계측", "측정", "데이터", "값", "현황")
            )
        ]

    analysis.schema_roles = roles[:10]
    kept = {role.role for role in analysis.schema_roles}
    analysis.join_requirements = [
        join
        for join in joins
        if join.from_role in kept and join.to_role in kept
    ][:10]
    return analysis


def _is_dimension_facility_query(query: str, analysis: QueryAnalysis) -> bool:
    """조직/시설 개수·목록 질의 — hist 시계열 단독 상위 억제 대상."""
    q = (query or "").casefold()
    intent = (analysis.intent or "").casefold()
    blob = f"{q} {intent}"
    dimension_hits = ("개수", "목록", "몇 개", "몇개", "리스트", "어디")
    measure_hits = ("계측", "측정", "평균", "합계", "값", "잔류", "수질", "태그값")
    if not any(term in blob for term in dimension_hits):
        return False
    if any(term in blob for term in measure_hits):
        return False
    role_blob = " ".join(
        f"{role.role} {' '.join(role.search_terms)}"
        for role in analysis.schema_roles
    ).casefold()
    hub_hits = ("사업장", "정수장", "본부", "조직", "시설", "마스터")
    return any(term in role_blob or term in blob for term in hub_hits)


def _apply_subject_area_ranking(
    tables: list[dict[str, Any]],
    *,
    dimension_facility: bool,
) -> list[dict[str, Any]]:
    """Prefer master/code hubs on dimension queries; demote hist/link hijacks."""
    if not tables or not dimension_facility:
        return tables
    adjusted: list[dict[str, Any]] = []
    for table in tables:
        item = dict(table)
        area = _resolve_subject_area(item)
        score = float(item.get("score") or 0.0)
        if area in {"hist", "link"}:
            score *= 0.35
        elif area == "master":
            score = min(1.0, score + 0.12)
        elif area == "code":
            score = min(1.0, score + 0.04)
        item["score"] = score
        item["subject_area"] = area
        adjusted.append(item)
    adjusted.sort(
        key=lambda item: (
            -float(item.get("score") or 0.0),
            str(item.get("original_name") or item.get("name") or "").lower(),
        )
    )
    return adjusted


def _role_candidate_score(
    role: SchemaRoleRequirement,
    table: dict[str, Any],
) -> float:
    base = float(table.get("score") or 0.0)
    role_text = " ".join([role.role, *role.search_terms]).lower()
    table_name = str(
        table.get("original_name") or table.get("name") or ""
    ).lower()
    table_text = " ".join(
        [
            table_name,
            str(table.get("description") or "").lower(),
            str(table.get("analyzed_description") or "").lower(),
        ]
    )
    tokens = {
        token
        for token in re.findall(r"[가-힣]{2,}|[a-z0-9_]{3,}", role_text)
        if token not in {"데이터", "테이블", "정보"}
    }
    matched = sum(1 for token in tokens if token in table_text)
    lexical = min(0.3, matched * 0.08)
    # Prefer exact physical hub tokens injected by role enrichment.
    for hub_token, hub_names in (
        ("rdisaup", ("rdisaup_tb",)),
        ("rdibonbu", ("rdibonbu_tb",)),
        ("rditag", ("rditag_tb",)),
        ("rdd01dd", ("rdd01dd_tb",)),
    ):
        if hub_token in role_text and any(name in table_name for name in hub_names):
            lexical = max(lexical, 0.55)
    temporal_signals = (
        (("15분", "십오분"), ("15분", "15mi")),
        (("시간별", "시간 단위", "매시간"), ("시간별", "시간 단위", "01hh")),
        (("일 단위", "일별", "하루"), ("일 단위", "일별", "01dd")),
    )
    for role_terms, table_terms in temporal_signals:
        if any(term in role_text for term in role_terms) and any(
            term in table_text for term in table_terms
        ):
            lexical = max(lexical, 0.45)
    area = _resolve_subject_area(table)
    hub_role = any(
        term in role_text
        for term in ("사업장", "정수장", "본부", "조직", "시설", "마스터", "코드")
    )
    if hub_role and area in {"hist", "link"}:
        return max(0.0, min(1.0, base + lexical) * 0.35)
    if hub_role and area == "master":
        lexical = max(lexical, 0.2)
    return min(1.0, base + lexical)


def _role_candidate_has_evidence(
    role: SchemaRoleRequirement,
    table: dict[str, Any],
) -> bool:
    role_text = " ".join([role.role, *role.search_terms]).lower()
    table_text = " ".join(
        [
            str(table.get("original_name") or table.get("name") or "").lower(),
            str(table.get("description") or "").lower(),
            str(table.get("analyzed_description") or "").lower(),
        ]
    )
    tokens = {
        token
        for token in re.findall(r"[가-힣]{2,}|[a-z0-9_]{3,}", role_text)
        if token not in {"데이터", "테이블", "정보", "마스터"}
    }
    if any(token in table_text for token in tokens):
        return True
    temporal_signals = (
        (("15분", "십오분"), ("15분", "15mi")),
        (("시간별", "시간 단위", "매시간"), ("시간별", "시간 단위", "01hh")),
        (("일 단위", "일별", "하루"), ("일 단위", "일별", "01dd")),
    )
    if any(
        any(term in role_text for term in role_terms)
        and any(term in table_text for term in table_terms)
        for role_terms, table_terms in temporal_signals
    ):
        return True
    area = _resolve_subject_area(table)
    if area == "agg" and any(
        term in role_text for term in ("계측", "측정", "시계열", "팩트", "값", "데이터")
    ):
        return True
    if area == "master" and any(
        term in role_text
        for term in ("사업장", "정수장", "본부", "태그", "마스터", "시설")
    ):
        return True
    return False


async def _resolve_filters(
    repository: PostgresMetadataRepository,
    *,
    requirements: list[FilterRequirement],
    embeddings: dict[str, list[float]],
    table_ids: list[int],
    tables_by_id: dict[int, dict[str, Any]],
    mappings: list[dict[str, Any]],
    minimum_similarity: float,
) -> tuple[list[PlannedFilter], list[str]]:
    planned: list[PlannedFilter] = []
    unresolved: list[str] = []
    for index, requirement in enumerate(requirements):
        embedding = embeddings.get(f"filter:{index}")
        best: dict[str, Any] | None = None
        if embedding is not None and table_ids:
            grouped = await repository.search_columns(
                embedding,
                table_ids=table_ids,
                per_table_limit=5,
            )
            options = [
                item
                for values in grouped.values()
                for item in values
            ]
            if options:
                best = max(
                    options,
                    key=lambda item: _filter_column_score(
                        requirement,
                        item,
                    ),
                )

        mapped = next(
            (
                mapping
                for mapping in mappings
                if requirement.value_text
                and str(mapping.get("natural_value") or "").lower()
                in requirement.value_text.lower()
            ),
            None,
        )
        # Prefer verified code mapping column over weak semantic column hits.
        if mapped is not None and mapped.get("column_fqn"):
            fqn = str(mapped["column_fqn"])
            parts = fqn.split(".")
            # Store seed uses db.schema.table.column; plan uses schema.table.column.
            column = (
                ".".join(parts[-3:])
                if len(parts) >= 3
                else fqn
            )
            value = str(mapped.get("code_value") or requirement.value_text)
            planned.append(
                PlannedFilter(
                    meaning=requirement.meaning,
                    column=column,
                    operator="EQ",
                    value=value,
                    resolution_status="resolved",
                    confidence=1.0,
                )
            )
            continue

        score = min(1.0, (
            _filter_column_score(requirement, best)
            if best
            else 0.0
        ))
        is_resolved = bool(best and score >= minimum_similarity)
        table = (
            tables_by_id.get(int(best["table_id"]))
            if best and best.get("table_id") is not None
            else None
        )
        column = (
            f"{table.get('schema_name')}.{table.get('original_name') or table.get('name')}."
            f"{best.get('name')}"
            if table and best
            else None
        )
        value = requirement.value_text
        planned.append(
            PlannedFilter(
                meaning=requirement.meaning,
                column=column or None,
                operator=requirement.operator_hint,
                value=value,
                resolution_status="resolved" if is_resolved else "unresolved",
                confidence=score,
            )
        )
        if requirement.required and not is_resolved:
            unresolved.append(f"필수 필터 컬럼 미해결: {requirement.meaning}")
    return planned, unresolved


async def decide(
    repository: PostgresMetadataRepository,
    *,
    query: str,
    include_matched_columns: bool,
    column_top_m: int | None,
    auto_resolve_entities: bool,
    table_limit: int | None = None,
) -> DecisionResponse:
    runtime = get_runtime()
    decision = runtime.decision
    effective_top_k = (
        max(1, min(50, int(table_limit)))
        if table_limit is not None
        else decision.table_top_k
    )

    analysis = await get_query_analyzer().analyze(query)
    analysis = _enrich_analysis_roles(query, analysis)
    axis_texts: dict[str, str] = {"question": query}
    if analysis.status == "complete":
        hyde_text = analysis_embedding_text(analysis)
        if hyde_text:
            axis_texts["hyde"] = hyde_text
        for role in analysis.schema_roles:
            axis_texts[f"role:{role.role}"] = role_embedding_text(
                analysis,
                role,
            )
        for index, requirement in enumerate(analysis.filter_requirements):
            axis_texts[f"filter:{index}"] = "\n".join(
                value
                for value in [
                    requirement.meaning,
                    requirement.value_text or "",
                    analysis.intent,
                ]
                if value
            )

    provider = get_embedding_provider()
    axis_names = list(axis_texts)
    vectors = await provider.embed_batch([axis_texts[name] for name in axis_names])
    embeddings = dict(zip(axis_names, vectors))

    axis_results: dict[str, list[dict[str, Any]]] = {
        "question": await repository.search_tables(
            embeddings["question"],
            limit=effective_top_k,
        )
    }
    if "hyde" in embeddings:
        axis_results["hyde"] = await repository.search_tables(
            embeddings["hyde"],
            limit=effective_top_k,
        )

    preliminary = merge_axis_candidates(
        axis_results,
        question_weight=decision.question_weight,
        hyde_weight=decision.hyde_weight,
        role_weight=decision.role_weight,
        limit=effective_top_k,
    )
    selected_source_instance = (
        str(preliminary[0].get("source_instance_id") or "")
        if preliminary
        else ""
    )

    mappings = await repository.find_value_mappings(query)
    if not selected_source_instance:
        selected_source_instance = _provisional_source_instance_id([], mappings)
    mappings = [
        mapping
        for mapping in mappings
        if selected_source_instance
        and _same_source(mapping, selected_source_instance)
    ]

    role_candidates: dict[str, list[dict[str, Any]]] = {}
    if selected_source_instance and analysis.status == "complete":
        for role in analysis.schema_roles:
            axis = f"role:{role.role}"
            vector = embeddings.get(axis)
            if vector is None:
                continue
            rows = await repository.search_tables(
                vector,
                limit=decision.role_top_k,
                source_instance_id=selected_source_instance,
            )
            rows = [
                {
                    **row,
                    "vector_score": float(row.get("score") or 0.0),
                    "score": _role_candidate_score(role, row),
                    "role_evidence": _role_candidate_has_evidence(role, row),
                }
                for row in rows
            ]
            rows = [
                row
                for row in rows
                if row["role_evidence"]
                or float(row["vector_score"]) >= decision.role_semantic_floor
            ]
            rows.sort(
                key=lambda row: (
                    -float(row.get("score") or 0.0),
                    str(row.get("original_name") or row.get("name") or ""),
                )
            )
            if rows:
                role_cutoff = (
                    float(rows[0].get("score") or 0.0)
                    * decision.role_min_score_ratio
                )
                rows = [
                    row
                    for row in rows
                    if float(row.get("score") or 0.0) >= role_cutoff
                ]
            role_candidates[role.role] = rows
            axis_results[axis] = rows

    merged = merge_axis_candidates(
        axis_results,
        question_weight=decision.question_weight,
        hyde_weight=decision.hyde_weight,
        role_weight=decision.role_weight,
        limit=max(effective_top_k, len(analysis.schema_roles) * decision.role_top_k),
    )
    merged = [
        table
        for table in merged
        if not selected_source_instance
        or str(table.get("source_instance_id") or "")
        == selected_source_instance
    ]

    mapped_table_ids = {
        int(mapping["table_id"])
        for mapping in mappings
        if mapping.get("table_id") is not None
    }
    existing_ids = {int(table["id"]) for table in merged}
    mapped_tables = await repository.fetch_tables_by_ids(
        mapped_table_ids - existing_ids
    )
    for table in mapped_tables:
        if (
            selected_source_instance
            and str(table.get("source_instance_id") or "")
            != selected_source_instance
        ):
            continue
        table["score"] = 1.0
        table["axis_scores"] = {"value_mapping": 1.0}
        table["role_scores"] = {}
        merged.append(table)
    # Exact value-mapping hubs already present from vector search must not
    # keep a weak vector score and then be gap-pruned away.
    for table in merged:
        if int(table["id"]) in mapped_table_ids:
            table["score"] = max(float(table.get("score") or 0.0), 1.0)
            axes = dict(table.get("axis_scores") or {})
            axes["value_mapping"] = 1.0
            table["axis_scores"] = axes

    dimension_facility = _is_dimension_facility_query(query, analysis)
    merged = _apply_subject_area_ranking(
        merged,
        dimension_facility=dimension_facility,
    )

    pruned = prune_by_score_gap(
        merged,
        max_k=effective_top_k,
        gap_ratio=decision.score_gap_ratio,
        min_step=decision.score_min_step,
        top_radius=decision.score_top_radius,
    )

    edge_rows = (
        await repository.fetch_join_edges(
            source_instance_id=selected_source_instance
        )
        if selected_source_instance
        else []
    )
    edges = build_composite_edges(edge_rows)
    required_roles = [
        role.role
        for role in analysis.schema_roles
        if role.necessity == "required"
    ]
    optional_roles = [
        role.role
        for role in analysis.schema_roles
        if role.necessity == "optional"
    ]
    selection = select_minimal_tables(
        required_roles=required_roles,
        optional_roles=optional_roles,
        role_candidates=role_candidates,
        edges=edges,
        max_hops=decision.fk_max_hops,
        table_limit=effective_top_k,
        distinct_role_pairs={
            frozenset({requirement.from_role, requirement.to_role})
            for requirement in analysis.join_requirements
            if requirement.required
        },
    )

    if analysis.status != "complete":
        selection.selected_table_ids = {
            int(table["id"]) for table in pruned
        }
        selection.bridge_table_ids = set()
        selection.paths = []
        selection.unresolved = [
            "의미 분해 실패로 역할별 최소 테이블 집합을 확정하지 못함"
        ]

    all_table_ids = {
        int(table["id"]) for table in pruned
    } | selection.selected_table_ids
    fetched = await repository.fetch_tables_by_ids(all_table_ids)
    tables_by_id = {
        int(table["id"]): table for table in [*merged, *fetched]
    }
    for table in pruned:
        tables_by_id[int(table["id"])].update(table)

    ordered_tables = [
        table
        for table in pruned
        if int(table["id"]) in all_table_ids
    ]
    ordered_ids = {int(table["id"]) for table in ordered_tables}
    for table_id in sorted(selection.selected_table_ids - ordered_ids):
        table = tables_by_id.get(table_id)
        if table is not None:
            table.setdefault("score", 0.0)
            ordered_tables.append(table)

    column_ids = [int(table["id"]) for table in ordered_tables]
    columns = (
        await repository.search_columns(
            embeddings["question"],
            table_ids=column_ids,
            per_table_limit=column_top_m or decision.column_top_m,
        )
        if include_matched_columns
        else {}
    )
    if include_matched_columns:
        metric = str(analysis.measurement.metric or "").strip()
        if metric:
            metric_columns = await repository.search_columns(
                await provider.embed(metric),
                table_ids=column_ids,
                per_table_limit=column_top_m or decision.column_top_m,
            )
            columns = _merge_column_hits(columns, metric_columns)
        for table in ordered_tables:
            table_id = int(table["id"])
            table_score = float(table.get("score") or 0.0)
            column_score = max(
                (
                    float(item.get("score") or 0.0)
                    for item in columns.get(table_id, [])
                ),
                default=table_score,
            )
            if table_id not in selection.selected_table_ids:
                table["score"] = 0.5 * table_score + 0.5 * column_score
        ordered_tables.sort(
            key=lambda item: (
                0 if int(item["id"]) in selection.selected_table_ids else 1,
                -float(item.get("score") or 0.0),
            )
        )

    plan_table_ids = sorted(selection.selected_table_ids)
    planned_filters, filter_unresolved = await _resolve_filters(
        repository,
        requirements=analysis.filter_requirements,
        embeddings=embeddings,
        table_ids=plan_table_ids,
        tables_by_id=tables_by_id,
        mappings=mappings,
        minimum_similarity=decision.minimum_similarity,
    )
    unresolved = [*selection.unresolved, *filter_unresolved]
    if analysis.status == "degraded":
        completeness = "degraded"
    elif not selection.role_tables and required_roles:
        completeness = "failed"
    elif unresolved:
        completeness = "partial"
    else:
        completeness = "complete"

    planned_paths = _planned_paths(selection.paths, tables_by_id)
    role_by_name = {role.role: role for role in analysis.schema_roles}
    required_columns_by_id: dict[int, set[str]] = defaultdict(set)
    table_id_by_name = {
        str(table.get("original_name") or table.get("name") or "").lower(): table_id
        for table_id, table in tables_by_id.items()
    }
    for path in planned_paths:
        for condition in path.conditions:
            for fqn in (condition.from_, condition.to):
                parts = fqn.split(".")
                if len(parts) >= 3:
                    table_id = table_id_by_name.get(parts[-2].lower())
                    if table_id is not None:
                        required_columns_by_id[table_id].add(parts[-1])
    for planned_filter in planned_filters:
        parts = (planned_filter.column or "").split(".")
        if len(parts) >= 3:
            table_id = table_id_by_name.get(parts[-2].lower())
            if table_id is not None:
                required_columns_by_id[table_id].add(parts[-1])

    planned_by_id: dict[int, PlannedTable] = {}
    for role, table in selection.role_tables.items():
        table_id = int(table["id"])
        current = planned_by_id.get(table_id)
        if current is None:
            current = PlannedTable(
                **_table_key(table).model_dump(),
                table_id=table_id,
                role=role,
                roles=[role],
                necessity=role_by_name[role].necessity,
                required_columns=sorted(required_columns_by_id[table_id]),
                score=float(table.get("score") or 0.0),
            )
            planned_by_id[table_id] = current
        elif role not in current.roles:
            current.roles.append(role)
            if role_by_name[role].necessity == "required":
                current.necessity = "required"
    planned_tables = list(planned_by_id.values())
    bridge_tables = [
        _table_key(tables_by_id[table_id])
        for table_id in sorted(selection.bridge_table_ids)
        if table_id in tables_by_id
    ]
    plan = QueryPlan(
        completeness=completeness,
        strategy=_strategy(planned_paths, planned_filters),
        required_tables=planned_tables,
        bridge_tables=bridge_tables,
        join_paths=planned_paths,
        filters=planned_filters,
        unresolved_requirements=unresolved,
    )

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
    join_groups = (
        [
            JoinGroup(
                members=[
                    _table_key(tables_by_id[table_id])
                    for table_id in sorted(selection.selected_table_ids)
                    if table_id in tables_by_id
                    and table_id not in selection.bridge_table_ids
                ],
                bridge_tables=bridge_tables,
                recommended_strategy=plan.strategy,
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
        if bridges
        else []
    )

    candidates = [
        _candidate(
            table,
            columns.get(int(table["id"]), []),
            source=(
                "name_rule"
                if int(table["id"]) in mapped_table_ids
                else "vector"
            ),
        )
        for table in ordered_tables[:effective_top_k]
    ]
    entities = _resolved_entities(mappings) if auto_resolve_entities else []
    allowed_objects = [
        str(
            tables_by_id[table_id].get("original_name")
            or tables_by_id[table_id].get("name")
            or ""
        )
        for table_id in plan_table_ids
        if table_id in tables_by_id
    ]
    execution_context = None
    if completeness == "complete" and selected_source_instance and allowed_objects:
        try:
            resolved = await resolve_execution_context(
                repository,
                source_instance_id=selected_source_instance,
                requested_objects=allowed_objects,
            )
            if resolved.source_name:
                execution_context = ExecutionContext(**resolved.public_dict())
            else:
                execution_context = None
        except ExecutionBindingError:
            execution_context = None

    secondary = sorted(
        {
            candidate.target_class
            for candidate in candidates
            if candidate.target_class
            not in {"unknown", _top_target(candidates)}
        }
    )
    return DecisionResponse(
        target=_top_target(candidates),
        secondary_targets=secondary,
        confidence=float(candidates[0].score) if candidates else 0.0,
        candidates=candidates,
        join_groups=join_groups,
        threshold_used={
            "minimum_similarity": decision.minimum_similarity,
            "table_top_k": effective_top_k,
            "table_limit": table_limit,
            "column_top_m": column_top_m or decision.column_top_m,
            "retrieval_axes": axis_names,
            "fk_max_hops": decision.fk_max_hops,
        },
        resolved_entities=entities,
        suggested_probes=[],
        resolution_status="complete" if entities else "partial",
        execution_context=execution_context,
        query_analysis=analysis,
        query_plan=plan,
    )
