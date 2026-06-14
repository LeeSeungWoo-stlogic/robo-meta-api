"""/data_decision 핵심 — 자산 ① 검색 파이프라인 호출 + v0.6 RC DecisionResponse 변환.

흐름 (자산 ① 그대로 재사용, K-AIR-meta-api 가 pgvector 로 했던 일을 Neo4j 벡터 인덱스로 수행):
  1) HyDE (자산 ① hyde.get_hyde_generator) — 질문 → 구조화 힌트.
  2) Embedding (자산 ① embedding.get_embedding_client) — HyDE 텍스트 + 원본 질문 → 1536-dim 벡터.
  3) Vector Search (자산 ① vector_search.search_tables_by_vector) — Neo4j text_to_sql_table_vec_index.
  4) 두 축(HyDE / 원본 질문) 가중 합산 병합.
  5) 컬럼 매칭(옵션) — 자산 ①의 fetch_anchor_columns 로 후보 컬럼 추출.
  6) subject_area / target_class 분류 → DecisionResponse 조립.

R-3 db 폴백 체인: Schema.db || DataSource.engine || settings.meta_db_label.
v0.6 RC 풍부 필드(join_groups 등)는 빈 값 폴백.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from neo4j import AsyncDriver

from ..config import settings
from ..schemas import (
    ColumnConstraint,
    DecisionCandidate,
    DecisionResponse,
    MatchedColumn,
    META_VERSION,
    TargetClass,
)
from . import subject_area as sa
from .entity_resolution import resolve_entities
from .neo4j_client.embedding import get_embedding_client
from .neo4j_client.hyde import build_hyde_embedding_text, get_hyde_generator
from .neo4j_client.models import TableCandidate
from .neo4j_client.vector_search import (
    build_convention_bridge_path,
    fetch_anchor_columns,
    fetch_convention_joins,
    fetch_fk_relationships,
    fetch_ontology_relationships,
    search_tables_by_vector,
    _convention_bridge_confidence,
)


def _subject_area_to_target_class(area: str) -> TargetClass:
    """K-AIR gen-1 과 동일 매핑 — schemas.py 의 enum 호환."""
    if area == "agg":
        return "analytic"
    if area in ("raw", "master", "code"):
        return "source"
    if area in ("hist", "link"):
        return "collect"
    return "unknown"


def _merge_candidates(
    hyde_tables: List[TableCandidate],
    question_tables: List[TableCandidate],
    *,
    top_k: int,
) -> List[TableCandidate]:
    """K-AIR `_merge_topk` 와 동일한 가중 점수 합산 (hyde_weight / question_weight 그대로)."""
    wh = settings.hyde_weight
    wq = settings.question_weight
    bucket: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def _add(rows: List[TableCandidate], weight: float) -> None:
        for t in rows:
            key = (t.schema or "", t.name or "")
            cur = bucket.get(key)
            if cur is None:
                bucket[key] = {"obj": t, "score": float(t.score) * weight}
            else:
                cur["score"] += float(t.score) * weight

    if hyde_tables:
        _add(hyde_tables, wh)
    _add(question_tables, wq if hyde_tables else 1.0)

    sorted_keys = sorted(bucket.keys(), key=lambda k: bucket[k]["score"], reverse=True)[:top_k]
    out: List[TableCandidate] = []
    for k in sorted_keys:
        t = bucket[k]["obj"]
        out.append(
            TableCandidate(
                schema=t.schema,
                name=t.name,
                description=t.description,
                analyzed_description=t.analyzed_description,
                score=float(bucket[k]["score"]),
            )
        )
    return out


async def _resolve_db_label(driver: AsyncDriver, *, schema_name: str) -> str:
    """R-3: Schema.db || DataSource.engine || META_DB_LABEL."""
    if not schema_name:
        return settings.meta_db_label
    cypher = """
    MATCH (s:Schema) WHERE toLower(s.name) = toLower($schema_name)
    OPTIONAL MATCH (ds:DataSource)-[:HAS_SCHEMA]->(s)
    RETURN COALESCE(s.db, ds.engine, $default_db) AS db LIMIT 1
    """
    async with driver.session() as sess:
        result = await sess.run(cypher, schema_name=schema_name, default_db=settings.meta_db_label)
        row = await result.single()
    if row and row["db"]:
        return str(row["db"])
    return settings.meta_db_label


async def _attach_matched_columns(
    driver: AsyncDriver,
    cands: List[TableCandidate],
    *,
    keywords: List[str],
    top_m: int,
) -> Dict[Tuple[str, str], List[MatchedColumn]]:
    """자산 ① fetch_anchor_columns 로 후보 컬럼을 받아 MatchedColumn 배열로 변환."""
    if not cands or top_m <= 0 or not keywords:
        return {}
    async with driver.session() as sess:
        cols = await fetch_anchor_columns(
            sess,
            tables=cands,
            keywords_lower=[k.lower() for k in keywords],
            per_table_limit=top_m,
        )
    out: Dict[Tuple[str, str], List[MatchedColumn]] = {}
    for c in cols:
        key = (c.table_schema or "", c.table_name or "")
        out.setdefault(key, []).append(
            MatchedColumn(
                column_name=c.name,
                score=float(c.score or 0.0),
                constraints=[],
                column_name_kr=None,
                data_type=c.dtype or None,
                description=c.description or None,
            )
        )
    return out


async def _decide_keyword(driver: AsyncDriver, *, query: str) -> List[TableCandidate]:
    """폴백: 공백 분리 토큰 OR Cypher CONTAINS (자산 ① 의 vector 인덱스 없이도 동작).

    K-AIR gen-1 의 `_decide_keyword` 와 동등 — 백엔드만 Neo4j Cypher 로 교체.
    """
    import re

    tokens = [t for t in re.findall(r"[가-힣a-zA-Z0-9_]{2,}", query)
              if t not in {"알려줘", "보여줘", "검색", "조회", "해줘", "어떻게", "얼마나", "무엇", "뭐야"}]
    tokens = [t.lower() for t in tokens][:10]
    if not tokens:
        return []
    cypher = """
    MATCH (t:Table)
    WHERE COALESCE(t.text_to_sql_db_exists, true) = true
    WITH t, [kw IN $tokens
             WHERE toLower(COALESCE(t.name, '')) CONTAINS kw
                OR toLower(COALESCE(t.description, '')) CONTAINS kw
                OR toLower(COALESCE(t.analyzed_description, '')) CONTAINS kw] AS hits
    WHERE size(hits) > 0
    RETURN t.schema AS schema, t.name AS name, t.description AS description,
           t.analyzed_description AS analyzed_description,
           toFloat(size(hits)) / toFloat($n) AS score
    ORDER BY score DESC, name ASC
    LIMIT $k
    """
    async with driver.session() as sess:
        result = await sess.run(
            cypher,
            tokens=tokens,
            n=len(tokens),
            k=settings.decision_topk,
        )
        rows = await result.data()
    return [
        TableCandidate(
            schema=str(r.get("schema") or ""),
            name=str(r.get("name") or ""),
            description=str(r.get("description") or ""),
            analyzed_description=str(r.get("analyzed_description") or ""),
            score=float(r.get("score") or 0.0),
        )
        for r in rows
    ]


def _extract_keywords(hyde_out: Any, question: str) -> List[str]:
    """자산 ① api._extract_keywords_from_hyde 와 동일 정책."""
    import re

    keywords: list[str] = []
    seen: set[str] = set()

    def _add(items):
        for x in items or []:
            s = str(x or "").strip()
            if s and s.lower() not in seen and len(s) >= 2:
                seen.add(s.lower())
                keywords.append(s)

    if hyde_out:
        _add(hyde_out.entities.include)
        _add(hyde_out.search_keywords.tables)
        _add(hyde_out.search_keywords.columns)
        _add(hyde_out.join_filter_hints.filter_column_meanings)

    tokens = re.findall(r"[가-힣a-zA-Z0-9_]{2,}", question)
    stopwords = {"알려줘", "보여줘", "검색", "조회", "해줘", "어떻게", "얼마나", "무엇", "뭐야"}
    for t in tokens:
        if t.lower() not in seen and t not in stopwords:
            seen.add(t.lower())
            keywords.append(t)
    return keywords[:20]


def _classify_target(cands: List[DecisionCandidate]) -> Tuple[str, float]:
    """K-AIR `_classify` 와 동일 — agg/raw 임계값 기반 target/confidence."""
    if not cands:
        return "none", 0.0
    areas: Dict[str, int] = {}
    for c in cands:
        a = c.subject_area
        areas[a] = areas.get(a, 0) + 1
    top = cands[0]
    agg_n = areas.get("agg", 0)
    raw_n = areas.get("raw", 0) + areas.get("master", 0) + areas.get("code", 0)
    if agg_n >= 1 and top.score >= settings.decision_analytic_threshold:
        return "analytic", top.score
    if raw_n >= 1 and top.score >= settings.decision_source_threshold:
        return "source", top.score
    return "collect", top.score


def _collect_secondary(top_target: str, cands: List[DecisionCandidate]) -> List[str]:
    valid = {"analytic", "source", "collect"}
    seen: List[str] = []
    for c in cands:
        tc = c.target_class
        if tc not in valid or tc == top_target:
            continue
        if tc not in seen:
            seen.append(tc)
    return seen


def _append_join_mode(current: str, segment: str) -> str:
    if current == "empty":
        return segment
    if segment in current.split("+"):
        return current
    return f"{current}+{segment}"


def _component_size(parent: dict, find_fn, fqn_lower: str) -> int:
    root = find_fn(fqn_lower)
    return sum(1 for k in parent if find_fn(k) == root)


async def decide(
    driver: AsyncDriver,
    *,
    query: str,
    query_embedding: Optional[List[float]] = None,
    include_matched_columns: bool = True,
    column_top_m: Optional[int] = None,
    auto_resolve_entities: bool = True,
) -> DecisionResponse:
    """v0.6 RC `/data_decision` 메인."""
    debug: Dict[str, Any] = {"mode": None}
    effective_col_m = column_top_m if column_top_m is not None else settings.decision_column_top_m
    effective_col_m = max(1, min(50, int(effective_col_m)))

    hyde_out = None
    hyde_status = "skipped"
    hyde_embedding: Optional[List[float]] = None
    question_embedding: Optional[List[float]] = query_embedding

    # 1) HyDE — 외부 임베딩이 주어지지 않은 경우만
    if query_embedding is None and settings.openai_enabled:
        hyde_gen = get_hyde_generator()
        hyde_out, hyde_status = await hyde_gen.generate(question=query)
        debug["hyde_status"] = hyde_status
        # 2) Embedding
        embedder = get_embedding_client()
        texts: List[str] = []
        if hyde_out is not None:
            et = build_hyde_embedding_text(hyde_out)
            if et:
                texts.append(et)
        texts.append(query)
        try:
            vecs = await embedder.embed_batch(texts)
        except Exception as exc:
            debug["embedding_error"] = str(exc)[:200]
            vecs = []
        if vecs:
            if hyde_out is not None and len(vecs) >= 2:
                hyde_embedding = vecs[0]
                question_embedding = vecs[-1]
            else:
                question_embedding = vecs[-1]
        debug["mode"] = "internal_hyde+vector"
    elif query_embedding is not None:
        debug["mode"] = "precomputed_vector"
    else:
        debug["mode"] = "keyword_only"
        debug["reason"] = "OPENAI_API_KEY not set"

    # 3) Vector Search
    schema_filter = settings.decision_schema_allowlist or None
    tables_hyde: List[TableCandidate] = []
    tables_question: List[TableCandidate] = []
    mode_hyde = ""
    mode_question = ""
    async with driver.session() as sess:
        if hyde_embedding is not None:
            tables_hyde, mode_hyde = await search_tables_by_vector(
                sess,
                embedding=hyde_embedding,
                k=settings.decision_topk,
                schema_filter=schema_filter,
            )
        if question_embedding is not None:
            tables_question, mode_question = await search_tables_by_vector(
                sess,
                embedding=question_embedding,
                k=settings.decision_topk,
                schema_filter=schema_filter,
            )
    debug["hyde_results"] = len(tables_hyde)
    debug["question_results"] = len(tables_question)
    debug["hyde_search_mode"] = mode_hyde
    debug["question_search_mode"] = mode_question

    merged = _merge_candidates(tables_hyde, tables_question, top_k=settings.decision_topk)
    debug["merged"] = len(merged)

    # 폴백: HyDE/임베딩 실패 또는 vector 검색 0 hit → keyword(Cypher CONTAINS)
    if not merged:
        merged = await _decide_keyword(driver, query=query)
        debug["fallback"] = "keyword_cypher"
        debug["merged"] = len(merged)

    # 4) candidates 조립 (db 폴백 + subject_area 분류)
    cands: List[DecisionCandidate] = []
    for t in merged:
        schema_name = t.schema or ""
        area = sa.classify(schema_name, t.name or "")
        db_label = await _resolve_db_label(driver, schema_name=schema_name)
        cands.append(
            DecisionCandidate(
                db=db_label,
                schema_name=schema_name,
                table_name=t.name or "",
                score=float(t.score),
                source="vector",
                target_class=_subject_area_to_target_class(area),
                subject_area=area,
                matched_columns=[],
            )
        )

    # 5) matched_columns 채움
    columns_mode = "skipped"
    if include_matched_columns and settings.decision_match_columns and cands:
        keywords = _extract_keywords(hyde_out, query)
        col_map = await _attach_matched_columns(
            driver,
            merged,
            keywords=keywords,
            top_m=effective_col_m,
        )
        for c in cands:
            key = (c.schema_name, c.table_name)
            c.matched_columns = col_map.get(key, [])
        columns_mode = "neo4j_anchor"
    elif not include_matched_columns:
        columns_mode = "disabled_request"
    elif not settings.decision_match_columns:
        columns_mode = "disabled_env"

    # 6) target / secondary / confidence
    target, conf = _classify_target(cands)
    secondary = _collect_secondary(target, cands)

    # 7) Union-Find 기반 join_groups 조립 (3단 fallback)
    join_groups = []
    join_groups_mode = "empty"

    from ..schemas import JoinBridge, TableKey, JoinGroup
    from collections import defaultdict

    cand_map = {}
    table_fqns = []
    for c in cands:
        s = (c.schema_name or "").strip()
        n = (c.table_name or "").strip()
        if n:
            fqn = f"{s}.{n}" if s else n
            cand_map[fqn.lower()] = c
            table_fqns.append(fqn)

    if table_fqns:
        parent = {k: k for k in cand_map.keys()}

        def find(x):
            if parent[x] != x:
                parent[x] = find(parent[x])
            return parent[x]

        def union(x, y):
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry

        bridges_all = []

        async with driver.session() as sess:
            fk_relations = await fetch_fk_relationships(
                sess, table_fqns=table_fqns, limit=settings.join_fk_limit
            )

            for rel in fk_relations:
                fs = rel.get("from_schema") or ""
                ft = rel.get("from_table") or ""
                ts = rel.get("to_schema") or ""
                tt = rel.get("to_table") or ""
                from_fqn = f"{fs}.{ft}" if fs else ft
                to_fqn = f"{ts}.{tt}" if ts else tt
                fk, tk = from_fqn.lower(), to_fqn.lower()
                if fk in parent and tk in parent:
                    union(fk, tk)
                    bridges_all.append({
                        "bridge": JoinBridge(**{
                            "from": from_fqn,
                            "to": to_fqn,
                            "via": "fk",
                            "path": [],
                            "confidence": 1.0,
                        }),
                        "from_key": fk,
                        "to_key": tk,
                    })

            if fk_relations:
                join_groups_mode = "fk_graph"

            if settings.join_ontology_enabled:
                isolated_fqns = [
                    fqn for fqn in table_fqns
                    if _component_size(parent, find, fqn.lower()) < 2
                ]
                onto_rels: list = []
                if len(isolated_fqns) >= 2:
                    onto_rels = await fetch_ontology_relationships(
                        sess,
                        table_fqns=isolated_fqns,
                        limit=settings.join_ontology_limit,
                    )
                    for rel in onto_rels:
                        fs = rel.get("from_schema") or ""
                        ft = rel.get("from_table") or ""
                        ts = rel.get("to_schema") or ""
                        tt = rel.get("to_table") or ""
                        conf = float(rel.get("confidence", 0.8))
                        rel_type = rel.get("rel_type", "ontology")
                        from_fqn = f"{fs}.{ft}" if fs else ft
                        to_fqn = f"{ts}.{tt}" if ts else tt
                        fk, tk = from_fqn.lower(), to_fqn.lower()
                        if fk in parent and tk in parent:
                            union(fk, tk)
                            bridges_all.append({
                                "bridge": JoinBridge(**{
                                    "from": from_fqn,
                                    "to": to_fqn,
                                    "via": "ontology",
                                    "path": [rel_type],
                                    "confidence": conf,
                                }),
                                "from_key": fk,
                                "to_key": tk,
                            })
                if onto_rels:
                    join_groups_mode = _append_join_mode(
                        join_groups_mode, "ontology"
                    )

            if settings.join_convention_enabled:
                still_isolated = [
                    fqn for fqn in table_fqns
                    if _component_size(parent, find, fqn.lower()) < 2
                ]
                conv_rels: list = []
                if len(still_isolated) >= 2:
                    conv_rels = await fetch_convention_joins(
                        sess,
                        table_fqns=still_isolated,
                        min_col_len=settings.join_convention_min_col_len,
                        limit=settings.join_convention_limit,
                    )
                    for rel in conv_rels:
                        fs = rel.get("from_schema") or ""
                        ft = rel.get("from_table") or ""
                        ts = rel.get("to_schema") or ""
                        tt = rel.get("to_table") or ""
                        col = rel.get("join_column", "")
                        from_dtype = rel.get("from_dtype") or ""
                        to_dtype = rel.get("to_dtype") or ""
                        conf, dtype_match = _convention_bridge_confidence(
                            from_dtype,
                            to_dtype,
                            match_conf=settings.join_convention_confidence,
                            mismatch_conf=settings.join_convention_confidence_mismatch,
                        )
                        bridge_path = build_convention_bridge_path(
                            col,
                            from_dtype,
                            to_dtype,
                            dtype_match=dtype_match,
                        )
                        from_fqn = f"{fs}.{ft}" if fs else ft
                        to_fqn = f"{ts}.{tt}" if ts else tt
                        fk, tk = from_fqn.lower(), to_fqn.lower()
                        if fk in parent and tk in parent:
                            union(fk, tk)
                            bridges_all.append({
                                "bridge": JoinBridge(**{
                                    "from": from_fqn,
                                    "to": to_fqn,
                                    "via": "convention",
                                    "path": bridge_path,
                                    "confidence": conf,
                                }),
                                "from_key": fk,
                                "to_key": tk,
                            })
                if conv_rels:
                    join_groups_mode = _append_join_mode(
                        join_groups_mode, "convention"
                    )

        groups_dict = defaultdict(list)
        for fqn_key in cand_map.keys():
            groups_dict[find(fqn_key)].append(cand_map[fqn_key])

        for root, members in groups_dict.items():
            if len(members) < 2:
                continue

            group_fqn_keys = set()
            for m in members:
                s = (m.schema_name or "").strip().lower()
                n = (m.table_name or "").strip().lower()
                group_fqn_keys.add(f"{s}.{n}" if s else n)

            group_bridges = [
                b["bridge"] for b in bridges_all
                if b["from_key"] in group_fqn_keys
                and b["to_key"] in group_fqn_keys
            ]
            if not group_bridges:
                continue

            member_keys = [
                TableKey(**{
                    "db": m.db,
                    "schema_name": m.schema_name or None,
                    "table_name": m.table_name,
                })
                for m in members
            ]
            dbs = {m.db for m in members if m.db}
            cross_db = len(dbs) > 1
            rationale = (
                "Detected bridges: "
                + ", ".join(
                    f"{gb.from_} -[{gb.via}]-> {gb.to}" for gb in group_bridges
                )
            )
            group_score = max(m.score for m in members) if members else 0.0

            join_groups.append(
                JoinGroup(
                    members=member_keys,
                    bridge_tables=[],
                    cross_db=cross_db,
                    recommended_strategy="simple_join",
                    bridges=group_bridges,
                    group_score=group_score,
                    score_breakdown={},
                    rationale=rationale,
                )
            )

    if not join_groups:
        join_groups_mode = "empty"

    threshold_used = {
        "analytic": settings.decision_analytic_threshold,
        "source": settings.decision_source_threshold,
        "topk": settings.decision_topk,
        "column_top_m": effective_col_m,
        "matched_columns_mode": columns_mode,
        "hyde_weight": settings.hyde_weight,
        "question_weight": settings.question_weight,
        "mode": debug.get("mode"),
        "hyde_status": debug.get("hyde_status"),
        "hyde_results": debug.get("hyde_results"),
        "question_results": debug.get("question_results"),
        "merged": debug.get("merged"),
        "fallback": debug.get("fallback"),
        "join_groups_mode": join_groups_mode,
        "meta_version": META_VERSION,
    }

    resolved_entities = []
    suggested_probes = []
    resolution_status = "skipped"
    if auto_resolve_entities and settings.entity_resolution_enabled:
        resolved_entities, suggested_probes, resolution_status = await resolve_entities(
            driver, query=query, hyde_out=hyde_out
        )

    return DecisionResponse(
        target=target,
        secondary_targets=secondary,
        confidence=conf,
        candidates=cands,
        join_groups=join_groups,
        threshold_used=threshold_used,
        resolved_entities=resolved_entities,
        suggested_probes=suggested_probes,
        resolution_status=resolution_status,
    )
