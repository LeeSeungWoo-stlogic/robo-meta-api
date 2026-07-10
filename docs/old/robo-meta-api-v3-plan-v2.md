# robo-meta-api v3 구현 계획서 (v2)

> 작성일: 2026-06-13  
> 이전 버전: `robo-meta-api-v3-plan.md` (v1)  
> v2 변경 요약: `isolated_fqns` bug fix, ontology 양방향 Cypher, config wiring 필수화, session 통합, convention dedup, **convention dtype mismatch 감지·confidence 분기**, Agent CAST 소비 규칙, 검증·테스트 보강  
> 목적: robo-meta-api-v2를 기반으로 **온톨로지 관계 기반 join_groups 3단 fallback**을 구현한 v3 생성  
> 대상 레포지토리: `c:\Users\user\Desktop\Vibe_coding_list\K-AIR_gitea\robo-meta-api-v2` (복사 후 작업)  
> Git 브랜치: `v0.3` (origin: `https://github.com/LeeSeungWoo-stlogic/robo-meta-api.git`)

---

## v1 대비 v2 수정 사항

| # | v1 문제 | v2 수정 |
|---|---------|---------|
| 1 | `isolated_fqns` OR 조건 → FK 묶인 root도 2단 대상 | singleton component만 필터 (`component_size < 2`) |
| 2 | ontology Cypher 단방향 `(t1)-[r]->(t2)` | 무방향 `(t1)-[r]-(t2)` + 방향 정규화 |
| 3 | Step 4 config ↔ Step 2 코드 disconnect | config **필수**, decision_service에 wiring 포함 |
| 4 | Neo4j session 3회 분리 | 1 session에서 3 query 순차 실행 |
| 5 | `join_groups_mode` 문자열 조작 fragile | `_append_mode()` helper |
| 6 | convention 동일 table pair 중복 bridge | table pair당 1 bridge (컬럼명 길이 longest 우선) |
| 7 | air-swmm "이식" 과장 | **영감** 수준 명시 (granularity 다름) |
| 8 | schemas Step 3 불필요 | v2에 `path`/`BridgeVia` 이미 존재 → **확인만** |
| 9 | curl smoke만 | `tests/smoke_v06.py` join_groups assertion 추가 |
| 10 | convention dtype 검증 누락 | Neo4j `Column.dtype` 비교 → path 노출, mismatch 시 confidence 하향 + `cast_recommended` |
| 11 | Agent CAST 힌트 없음 | `docs/agent_join_hints.md` 소비 규칙 문서화 |

---

## 배경 및 목적

### 현재 v2의 한계

`robo-meta-api-v2`의 `/data_decision` 엔드포인트는 벡터 검색으로 후보 테이블을 선별한 뒤,
Neo4j의 `(Column)-[:fkTo]->(Column)` 관계만 탐색하여 `join_groups`를 조립한다.

```
현재 v2 동작:
  fkTo 관계 있음 → JoinGroup 조립
  fkTo 관계 없음 → join_groups = []   ← 문제
```

추가 v2 구조적 한계: `fk_relations`가 0건이면 Union-Find 자체를 skip한다.
v3에서는 후보 테이블이 있으면 **항상** Union-Find를 초기화하고 3단 fallback을 시도한다.

RWIS K-Water 환경에서 Neo4j에 `fkTo` 관계가 적재되지 않은 테이블 쌍이 많기 때문에
외부 AI가 받는 `join_groups`가 대부분 빈 배열(`[]`)이 되어 JOIN 힌트로서의 가치가 없다.

### 참고한 해결 방식 (영감 수준)

`KAIR/air-swmm` NLQ 서비스(`cross_source_join.py`, `hybrid_aggregate.py`)는
Neo4j 온톨로지 관계(`belongsToSystem`, `feedsTo` 등)를 Cypher로 탐색하여 cross-source JOIN을 해결한다.

> **주의**: air-swmm은 주로 **컬럼/시스템 수준** 온톨로지를 탐색한다.
> v3 2단은 **테이블↔테이블 직접 엣지**(`fkToTable`, `RELATED_TO` 등)를 대상으로 한다.
> 코드 포팅이 아니라 **fallback 전략 영감**이다.

### v3 목표

`join_groups`를 **3단 fallback**으로 구성하여,
FK가 없는 환경에서도 온톨로지 관계 및 컬럼명 패턴으로 JOIN 힌트를 제공한다.

```
v3 동작:
  1단: fkTo 관계 탐색           → BridgeVia="fk"         (기존 유지)
  2단: 테이블 수준 온톨로지 관계  → BridgeVia="ontology"   (신규, config on/off)
  3단: 공통 컬럼명 패턴 매칭     → BridgeVia="convention"  (신규, config on/off)
       ├─ dtype 일치            → confidence=0.5 (기본)
       └─ dtype 불일치/미상     → confidence=0.35 + path에 cast_recommended
  모두 없음                     → join_groups=[]
```

> **3단 dtype 정책**: 레거시 통합 환경에서 동일 컬럼명·상이 dtype(VARCHAR↔NUMERIC) 빈번.
> API는 dtype mismatch **감지·신호 전달**까지 담당. CAST SQL 생성은 **외부 AI Agent** 역할.

---

## 작업 전 필수 확인 사항

> **다른 모델이 이 계획서를 실행하기 전에 반드시 확인할 것**

### 0-1. Neo4j 테이블 수준 관계 현황

```cypher
-- 방향 무관 테이블 간 관계 타입 조회
MATCH (t1:Table)-[r]-(t2:Table)
WHERE id(t1) < id(t2)
RETURN DISTINCT type(r) AS rel_type, count(*) AS cnt
ORDER BY cnt DESC
```

- 결과에 `fkToTable`, `FK_TO_TABLE`, `RELATED_TO` 등이 있으면 2단 활용 가능.
- **0건이면** `JOIN_ONTOLOGY_ENABLED=0` 설정 후 3단만 사용.

Step 1-1의 `type(r) IN [...]` 목록을 위 결과에 맞게 조정할 것.

### 0-2. 컬럼명 공통 분포

```cypher
MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)
WHERE COALESCE(t.text_to_sql_db_exists, true) = true
WITH toLower(c.name) AS col_name, count(DISTINCT t.name) AS table_cnt
WHERE table_cnt >= 2
  AND length(col_name) >= 4
RETURN col_name, table_cnt
ORDER BY table_cnt DESC
LIMIT 30
```

결과로 `_CONVENTION_EXCLUDE`, `JOIN_CONVENTION_MIN_COL_LEN` 조정.

### 0-3. v2 스키마 확인 (변경 불필요 예상)

`app/schemas.py`에서 아래가 **이미 존재**하는지 확인:

```python
BridgeVia = Literal["fk", "ontology", "embedding", "term", "convention"]

class JoinBridge(BaseModel):
    path: List[str] = Field(default_factory=list, ...)
```

존재하면 Step 4(schemas)는 skip.

### 0-4. Column.dtype 적재율 확인

```cypher
MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)
WHERE COALESCE(t.text_to_sql_db_exists, true) = true
RETURN
  count(c) AS total_cols,
  count(c.dtype) AS cols_with_dtype,
  round(100.0 * count(c.dtype) / count(c), 1) AS dtype_pct
```

- `dtype_pct` ≥ 80% → 3단 dtype 분기 신뢰 가능.
- 50% 미만 → mismatch 감지 효과 제한적. `path`에 `dtype:unknown` 빈출 예상 — Agent는 `/meta/column`으로 보완.

---

## 작업 절차

### Step 0. v2 복사하여 v3 디렉토리 생성

```powershell
Copy-Item -Recurse `
  "c:\Users\user\Desktop\Vibe_coding_list\K-AIR_gitea\robo-meta-api-v2" `
  "c:\Users\user\Desktop\Vibe_coding_list\K-AIR_gitea\robo-meta-api-v3"

Remove-Item -Recurse -Force `
  "c:\Users\user\Desktop\Vibe_coding_list\K-AIR_gitea\robo-meta-api-v3\.git" -ErrorAction SilentlyContinue
```

이후 모든 작업은 `robo-meta-api-v3` 디렉토리에서만 진행한다.

---

### Step 1. `config.py` — 3단 정책 환경변수 추가 (필수)

파일 경로: `app/config.py`  
`Settings` dataclass 내 decision 정책 블록 아래에 추가:

```python
    # --- join_groups 3단 fallback 정책 ---
    join_ontology_enabled: bool = _env("JOIN_ONTOLOGY_ENABLED", "1").strip().lower() in (
        "1", "true", "yes", "y",
    )
    join_convention_enabled: bool = _env("JOIN_CONVENTION_ENABLED", "1").strip().lower() in (
        "1", "true", "yes", "y",
    )
    join_convention_min_col_len: int = int(_env("JOIN_CONVENTION_MIN_COL_LEN", "4"))
    join_convention_confidence: float = float(_env("JOIN_CONVENTION_CONFIDENCE", "0.5"))
    join_convention_confidence_mismatch: float = float(
        _env("JOIN_CONVENTION_CONFIDENCE_MISMATCH", "0.35")
    )
```

| 환경변수 | 기본값 | 의미 |
|----------|--------|------|
| `JOIN_CONVENTION_CONFIDENCE` | `0.5` | dtype **일치** convention bridge |
| `JOIN_CONVENTION_CONFIDENCE_MISMATCH` | `0.35` | dtype **불일치** 또는 한쪽 dtype 미상 |

v3 기본 포트:

```python
    api_port: int = int(_env("API_PORT", "8099"))
```

---

### Step 2. `vector_search.py` — 신규 함수 2개 + helper

파일 경로: `app/services/neo4j_client/vector_search.py`

#### 2-1. 관계 타입 상수 (Step 0 결과로 조정)

```python
# Step 0 Cypher 결과에 맞게 조정
_ONTOLOGY_REL_TYPES = [
    "fkToTable", "FK_TO_TABLE", "RELATED_TO",
    "REFERENCES", "DOMAIN_RELATED",
]
```

#### 2-2. `fetch_ontology_relationships()` (2단)

기존 `fetch_fk_relationships()` 아래에 추가.
**v2 fix: 무방향 탐색 + from/to 정규화 (fqn 사전순)**

```python
async def fetch_ontology_relationships(
    session,
    *,
    table_fqns: list[str],
    limit: int = 50,
) -> list[dict]:
    """2단: 테이블 수준 온톨로지 관계 탐색 (양방향).

    반환 형식:
        [{"from_schema", "from_table", "to_schema", "to_table",
          "rel_type", "confidence"}, ...]
    """
    if not table_fqns:
        return []

    cypher = """
    MATCH (t1:Table)-[r]-(t2:Table)
    WHERE type(r) IN $rel_types
    WITH t1, t2, r,
         (CASE WHEN t1.schema IS NOT NULL AND t1.schema <> ''
               THEN toLower(t1.schema) + '.' + toLower(t1.name)
               ELSE toLower(t1.name) END) AS fqn1,
         (CASE WHEN t2.schema IS NOT NULL AND t2.schema <> ''
               THEN toLower(t2.schema) + '.' + toLower(t2.name)
               ELSE toLower(t2.name) END) AS fqn2
    WHERE fqn1 IN $fqns AND fqn2 IN $fqns AND fqn1 < fqn2
    RETURN
      COALESCE(t1.schema,'') AS from_schema,
      COALESCE(t1.name,'')   AS from_table,
      COALESCE(t2.schema,'') AS to_schema,
      COALESCE(t2.name,'')   AS to_table,
      type(r)                AS rel_type,
      COALESCE(r.confidence, 0.8) AS confidence
    LIMIT $limit
    """
    fqns_lower = [f.lower() for f in table_fqns]
    result = await session.run(
        cypher,
        fqns=fqns_lower,
        rel_types=_ONTOLOGY_REL_TYPES,
        limit=limit,
    )
    return await result.data()
```

> `fqn1 < fqn2`로 중복 edge 방지. `from`/`to`는 fqn 사전순 기준.

#### 2-3. dtype 정규화 helper

`app/services/neo4j_client/api.py`의 base-type 추출과 동일 패턴:

```python
def _normalize_dtype(dtype: str | None) -> str:
    """varchar(50) → varchar, NUMERIC → numeric"""
    d = str(dtype or "").lower().strip()
    return d.split("(")[0].strip()


def _convention_bridge_confidence(
    from_dtype: str | None,
    to_dtype: str | None,
    *,
    match_conf: float,
    mismatch_conf: float,
) -> tuple[float, bool]:
    """(confidence, dtype_match) 반환."""
    nf, nt = _normalize_dtype(from_dtype), _normalize_dtype(to_dtype)
    if nf and nt:
        return (match_conf, True) if nf == nt else (mismatch_conf, False)
    # 한쪽 또는 양쪽 dtype 미상 → mismatch 취급 (보수적)
    return mismatch_conf, False
```

#### 2-4. `fetch_convention_joins()` (3단)

config에서 min_len/excludes를 받도록 파라미터화.
**dtype 필드 RETURN 추가** (Neo4j `Column.dtype` 활용):

```python
_CONVENTION_EXCLUDE = {
    "id", "seq", "yn", "use_yn", "del_yn", "reg_dt", "upd_dt",
    "reg_id", "upd_id", "remark", "memo", "note",
}

async def fetch_convention_joins(
    session,
    *,
    table_fqns: list[str],
    min_col_len: int = 4,
    excludes: set[str] | None = None,
    limit: int = 30,
) -> list[dict]:
    """3단: 동일 컬럼명 convention JOIN 힌트.

    table pair당 1 row (가장 긴 컬럼명 우선 — JOIN 키 specificity).
    dtype은 비교·path 구성용으로 함께 반환 (필터 제외 — mismatch도 bridge 후보).
    """
    if not table_fqns or len(table_fqns) < 2:
        return []

    ex = excludes or _CONVENTION_EXCLUDE

    cypher = """
    UNWIND $fqns AS fqn1
    UNWIND $fqns AS fqn2
    WITH fqn1, fqn2 WHERE fqn1 < fqn2
    MATCH (t1:Table)-[:HAS_COLUMN|hasColumn]->(c1:Column)
    WHERE (CASE WHEN t1.schema IS NOT NULL AND t1.schema <> ''
                THEN toLower(t1.schema) + '.' + toLower(t1.name)
                ELSE toLower(t1.name) END) = fqn1
    MATCH (t2:Table)-[:HAS_COLUMN|hasColumn]->(c2:Column)
    WHERE (CASE WHEN t2.schema IS NOT NULL AND t2.schema <> ''
                THEN toLower(t2.schema) + '.' + toLower(t2.name)
                ELSE toLower(t2.name) END) = fqn2
      AND toLower(c1.name) = toLower(c2.name)
      AND size(c1.name) >= $min_len
      AND NOT toLower(c1.name) IN $excludes
    WITH fqn1, fqn2, t1, t2,
         c1.name AS join_column,
         COALESCE(c1.dtype, '') AS from_dtype,
         COALESCE(c2.dtype, '') AS to_dtype
    ORDER BY size(join_column) DESC
    WITH fqn1, fqn2, t1, t2,
         collect(join_column)[0] AS join_column,
         collect(from_dtype)[0] AS from_dtype,
         collect(to_dtype)[0] AS to_dtype
    RETURN
      COALESCE(t1.schema,'') AS from_schema,
      COALESCE(t1.name,'')   AS from_table,
      join_column,
      from_dtype,
      to_dtype,
      COALESCE(t2.schema,'') AS to_schema,
      COALESCE(t2.name,'')   AS to_table
    LIMIT $limit
    """
    fqns_lower = [f.lower() for f in table_fqns]
    result = await session.run(
        cypher,
        fqns=fqns_lower,
        min_len=min_col_len,
        excludes=list(ex),
        limit=limit,
    )
    return await result.data()
```

> **설계 의도**: dtype 불일치 bridge를 **제외하지 않음** (레거시에서 의도적 VARCHAR↔NUMERIC join 존재).
> 대신 `confidence` 하향 + `path`에 dtype·cast_recommended로 Agent에 신호 전달.

#### 2-5. convention bridge path 빌더

`decision_service.py` 또는 `vector_search.py`에 공유 helper:

```python
def build_convention_bridge_path(
    col: str,
    from_dtype: str | None,
    to_dtype: str | None,
    *,
    dtype_match: bool,
) -> list[str]:
    path = [f"shared_column:{col}"]
    fd = from_dtype or "unknown"
    td = to_dtype or "unknown"
    path.append(f"dtype:{fd}↔{td}")
    if not dtype_match:
        path.append("cast_recommended:true")
    return path
```

---

### Step 3. `decision_service.py` — join_groups 3단 fallback

파일 경로: `app/services/decision_service.py`

#### 3-1. import 추가

```python
from .neo4j_client.vector_search import (
    fetch_anchor_columns,
    fetch_fk_relationships,
    fetch_ontology_relationships,
    fetch_convention_joins,
    build_convention_bridge_path,
    _convention_bridge_confidence,
    search_tables_by_vector,
)
```

#### 3-2. module-level helper (join_groups_mode)

```python
def _append_join_mode(current: str, segment: str) -> str:
    if current == "empty":
        return segment
    if segment in current.split("+"):
        return current
    return f"{current}+{segment}"
```

#### 3-3. component size helper

```python
def _component_size(parent: dict, find_fn, fqn_lower: str) -> int:
    root = find_fn(fqn_lower)
    return sum(1 for k in parent if find_fn(k) == root)
```

#### 3-4. `decide()` 내 join_groups 블록 교체

현재 코드(L368~471)를 아래로 교체:

```python
    # ──────────────────────────────────────────────
    # 7) Union-Find 기반 join_groups 조립 (3단 fallback)
    # ──────────────────────────────────────────────
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
            # ── 1단: fkTo (Column FK) ────────────────────────
            fk_relations = await fetch_fk_relationships(
                sess, table_fqns=table_fqns, limit=50
            )

            for rel in fk_relations:
                fs = rel.get("from_schema") or ""
                ft = rel.get("from_table") or ""
                ts = rel.get("to_schema") or ""
                tt = rel.get("to_table") or ""
                from_fqn = f"{fs}.{ft}" if fs else ft
                to_fqn   = f"{ts}.{tt}" if ts else tt
                fk, tk = from_fqn.lower(), to_fqn.lower()
                if fk in parent and tk in parent:
                    union(fk, tk)
                    bridges_all.append({
                        "bridge": JoinBridge(**{
                            "from": from_fqn, "to": to_fqn,
                            "via": "fk", "path": [], "confidence": 1.0,
                        }),
                        "from_key": fk, "to_key": tk,
                    })

            if fk_relations:
                join_groups_mode = "fk_graph"

            # ── 2단: ontology (singleton component only) ─────
            if settings.join_ontology_enabled:
                isolated_fqns = [
                    fqn for fqn in table_fqns
                    if _component_size(parent, find, fqn.lower()) < 2
                ]
                if len(isolated_fqns) >= 2:
                    onto_rels = await fetch_ontology_relationships(
                        sess, table_fqns=isolated_fqns, limit=50
                    )
                    for rel in onto_rels:
                        fs = rel.get("from_schema") or ""
                        ft = rel.get("from_table") or ""
                        ts = rel.get("to_schema") or ""
                        tt = rel.get("to_table") or ""
                        conf = float(rel.get("confidence", 0.8))
                        rel_type = rel.get("rel_type", "ontology")
                        from_fqn = f"{fs}.{ft}" if fs else ft
                        to_fqn   = f"{ts}.{tt}" if ts else tt
                        fk, tk = from_fqn.lower(), to_fqn.lower()
                        if fk in parent and tk in parent:
                            union(fk, tk)
                            bridges_all.append({
                                "bridge": JoinBridge(**{
                                    "from": from_fqn, "to": to_fqn,
                                    "via": "ontology",
                                    "path": [rel_type],
                                    "confidence": conf,
                                }),
                                "from_key": fk, "to_key": tk,
                            })
                    if onto_rels:
                        join_groups_mode = _append_join_mode(
                            join_groups_mode, "ontology"
                        )

            # ── 3단: convention (still singleton) ────────────
            if settings.join_convention_enabled:
                still_isolated = [
                    fqn for fqn in table_fqns
                    if _component_size(parent, find, fqn.lower()) < 2
                ]
                if len(still_isolated) >= 2:
                    conv_rels = await fetch_convention_joins(
                        sess,
                        table_fqns=still_isolated,
                        min_col_len=settings.join_convention_min_col_len,
                        limit=30,
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
                            from_dtype, to_dtype,
                            match_conf=settings.join_convention_confidence,
                            mismatch_conf=settings.join_convention_confidence_mismatch,
                        )
                        bridge_path = build_convention_bridge_path(
                            col, from_dtype, to_dtype, dtype_match=dtype_match,
                        )
                        from_fqn = f"{fs}.{ft}" if fs else ft
                        to_fqn   = f"{ts}.{tt}" if ts else tt
                        fk, tk = from_fqn.lower(), to_fqn.lower()
                        if fk in parent and tk in parent:
                            union(fk, tk)
                            bridges_all.append({
                                "bridge": JoinBridge(**{
                                    "from": from_fqn, "to": to_fqn,
                                    "via": "convention",
                                    "path": bridge_path,
                                    "confidence": conf,
                                }),
                                "from_key": fk, "to_key": tk,
                            })
                    if conv_rels:
                        join_groups_mode = _append_join_mode(
                            join_groups_mode, "convention"
                        )

        # ── JoinGroup 조립 ─────────────────────────────────
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
                TableKey(**{"db": m.db, "schema_name": m.schema_name or None,
                            "table_name": m.table_name})
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
```

> `_append_join_mode`, `_component_size`는 `decide()` 바로 위 module-level에 배치.

---

### Step 4. `schemas.py` — 확인만

v2에 `JoinBridge.path`, `BridgeVia`(`"ontology"`, `"convention"`) 이미 있으면 **수정 없음**.

---

### Step 5. `docker-compose.yml` 포트·환경변수

```yaml
services:
  robo-meta-api-v3:
    build: .
    container_name: robo-meta-api-v3
    ports:
      - "8099:8099"
    environment:
      - API_PORT=8099
      - JOIN_ONTOLOGY_ENABLED=1
      - JOIN_CONVENTION_ENABLED=1
      - JOIN_CONVENTION_MIN_COL_LEN=4
      - JOIN_CONVENTION_CONFIDENCE=0.5
      - JOIN_CONVENTION_CONFIDENCE_MISMATCH=0.35
```

---

### Step 6. `README.md` 업데이트

- 제목: `robo-meta-api v3`
- 포트: `8099`
- join_groups 3단 fallback 설명
- 환경변수: `JOIN_ONTOLOGY_ENABLED`, `JOIN_CONVENTION_ENABLED`, `JOIN_CONVENTION_MIN_COL_LEN`, `JOIN_CONVENTION_CONFIDENCE`, `JOIN_CONVENTION_CONFIDENCE_MISMATCH`
- convention bridge `path` 필드 해석 (`shared_column:`, `dtype:`, `cast_recommended:`)
- 알려진 제약: convention name-only 매칭 한계, CAST는 Agent 책임

---

### Step 6-1. `docs/agent_join_hints.md` — AI Agent 소비 규칙 (신규)

외부 NLQ/SQL Agent가 `/data_decision` 응답의 `join_groups`를 사용할 때 참고할 규칙.
README에서 링크.

```markdown
# join_groups Bridge 소비 규칙 (AI Agent용)

## via 우선순위
1. `fk` (confidence 1.0) — 최우선
2. `ontology` (confidence ~0.8)
3. `convention` (confidence ≤ 0.5) — 참고용

## convention bridge 처리

### confidence 분기
| 조건 | confidence | Agent 행동 |
|------|------------|------------|
| dtype 일치 (`path`에 `cast_recommended` 없음) | ~0.5 | 일반 equi-join |
| dtype 불일치 (`cast_recommended:true` in path) | ~0.35 | **CAST/`::type` 검토 필수** |
| confidence ≤ 0.5 전체 | — | 자동 join 금지, 사용자 확인 또는 `/meta/column` 재조회 권장 |

### path 토큰 해석
- `shared_column:{name}` — JOIN 키 후보 컬럼명
- `dtype:{left}↔{right}` — 양쪽 raw dtype (예: `varchar↔numeric`)
- `cast_recommended:true` — 타입 불일치. JOIN 전 양쪽 cast 정렬 필요

### CAST 예시 (PostgreSQL)
path: `dtype:varchar↔numeric`, shared_column: `plant_cd`
→ `t1.plant_cd::numeric = t2.plant_cd` 또는 `t1.plant_cd = t2.plant_cd::text`
(값 도메인·leading zero 여부에 따라 방향 선택)

### dtype unknown
`dtype:unknown↔numeric` 등 — `/meta/column` API로 실제 dtype 확인 후 cast 결정.
```

---

### Step 7. `tests/smoke_v06.py` — join_groups assertion 추가

`/data_decision` smoke 응답에서:

```python
# join_groups_mode가 empty가 아니거나, join_groups 길이 >= 0 (환경 의존)
mode = resp["threshold_used"].get("join_groups_mode", "empty")
assert "join_groups" in resp
# FK/ontology/convention 중 하나라도 있으면 via 값 검증
for jg in resp.get("join_groups", []):
    for b in jg.get("bridges", []):
        assert b["via"] in ("fk", "ontology", "convention")
        if b["via"] == "convention":
            assert b["confidence"] <= 0.6
            assert any(p.startswith("shared_column:") for p in b.get("path", []))
            assert any(p.startswith("dtype:") for p in b.get("path", []))
            # cast_recommended 있으면 confidence 더 낮아야 함
            if any(p == "cast_recommended:true" for p in b.get("path", [])):
                assert b["confidence"] <= 0.5
```

환경에 FK/ontology 없으면 `join_groups=[]`도 pass.

---

### Step 8. Git — v0.3 브랜치 (명시적 요청 시만)

```powershell
git -C "c:\Users\user\Desktop\Vibe_coding_list\K-AIR_gitea\robo-meta-api" checkout -b v0.3

robocopy `
  "c:\Users\user\Desktop\Vibe_coding_list\K-AIR_gitea\robo-meta-api-v3" `
  "c:\Users\user\Desktop\Vibe_coding_list\K-AIR_gitea\robo-meta-api" `
  /E /XD ".git" "__pycache__" "logs" /XF ".env"

git -C "c:\Users\user\Desktop\Vibe_coding_list\K-AIR_gitea\robo-meta-api" add -A
git -C "c:\Users\user\Desktop\Vibe_coding_list\K-AIR_gitea\robo-meta-api" `
  commit -m "v3: join_groups 3-tier fallback (fk, ontology, convention)"
# push는 사용자 요청 시
```

---

## 검증 계획

### V-1. Docker 기동

```bash
docker compose up -d --build
curl http://127.0.0.1:8099/health
```

### V-2. `/data_decision` 통합

질의: `"유역본부 내 정수장별 공급량은?"`

| 항목 | v2 | v3 |
|------|----|----|
| `join_groups` (FK 없을 때) | `[]` | ≥1 (ontology or convention) |
| `bridges[].via` | `"fk"` or 없음 | `"fk"` / `"ontology"` / `"convention"` |
| `join_groups_mode` | `"empty"` / `"fk_graph"` | `"fk_graph+ontology+convention"` 등 |

### V-3. 단계별 독립 검증

| 단계 | 방법 | 기대 |
|------|------|------|
| 2단 ontology | Neo4j에 `fkToTable` 1건 삽입 | `via=ontology`, mode에 `ontology` |
| 3단 convention (dtype match) | `JOIN_ONTOLOGY_ENABLED=0`, 동일 dtype 공유 컬럼 | `via=convention`, `confidence≈0.5`, path에 `dtype:` only |
| 3단 convention (dtype mismatch) | VARCHAR↔NUMERIC 공유 컬럼 테이블 쌍 | `confidence≈0.35`, path에 `cast_recommended:true` |
| exclude | `id`/`seq` 공통 컬럼 테이블 | bridge 없음 |
| singleton skip | FK로 2테이블 묶인 후 3단 | isolated 아닌 테이블은 convention query 제외 |

### V-4. smoke test

```bash
python tests/smoke_v06.py
```

---

## 파일별 변경 요약

| 파일 | 변경 | 내용 |
|------|------|------|
| `app/config.py` | **수정** | join fallback env vars, port 8099 |
| `app/services/neo4j_client/vector_search.py` | **수정** | ontology/convention fetch, dtype helper, path builder |
| `app/services/decision_service.py` | **수정** | 3단 fallback + dtype confidence 분기 |
| `app/schemas.py` | **확인** | 변경 없을 가능성 높음 |
| `docker-compose.yml` | **수정** | port 8099, env vars |
| `README.md` | **수정** | v3 docs, path 토큰 설명 |
| `docs/agent_join_hints.md` | **추가** | AI Agent CAST 소비 규칙 |
| `tests/smoke_v06.py` | **수정** | join_groups + dtype path assertion |
| `docs/` | **추가** | 본 plan v2 복사 |

---

## 주의사항

> [!IMPORTANT]
> **v2(`robo-meta-api-v2`) 디렉토리 수정 금지.** `robo-meta-api-v3` 복사본에서만 작업.

> [!WARNING]
> **convention bridge는 name-based 힌트.** dtype mismatch 시 `confidence≈0.35` + `cast_recommended:true`.
> CAST SQL 생성은 API 범위 밖 — Agent는 `docs/agent_join_hints.md` 규칙 따를 것.
> `_CONVENTION_EXCLUDE` Step 0 결과로 조정.

> [!NOTE]
> **dtype 미상(`unknown`)은 mismatch 취급** (보수적). Step 0-4 적재율 낮으면 Agent가 `/meta/column` 보완.
> **2단 = Neo4j table-level rel 품질 의존.** Step 0에서 0건 → `JOIN_ONTOLOGY_ENABLED=0`.

> [!NOTE]
> **Git push/commit은 사용자 명시 요청 시만.**

---

## 실행 순서 (executor checklist)

```
[ ] Step 0-1~0-4 Cypher + schema 확인
[ ] Step 0 v2 → v3 copy
[ ] Step 1 config.py (confidence + mismatch env)
[ ] Step 2 vector_search.py (2 fn + dtype helpers + path builder)
[ ] Step 3 decision_service.py (replace L368~471, dtype 분기)
[ ] Step 4 schemas confirm
[ ] Step 5 docker-compose
[ ] Step 6 README + Step 6-1 agent_join_hints.md
[ ] Step 7 smoke test (dtype path assertion)
[ ] V-1 ~ V-4 검증 (V-3 dtype mismatch case 포함)
[ ] Step 8 git (요청 시)
```
