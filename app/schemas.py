"""계약 Pydantic 모델 (v0.5 → v0.6 확장).

v0.6 변경 요약:
- 응답에 `meta_version` 동봉
- /data_decision: candidate별 `target_class`/`subject_area`/`matched_columns`,
  최상위 `secondary_targets`/`join_groups` 추가
- /meta/table: `table_info.datasource`/`lineage_brief`/`ontology_anchors`,
  `columns[].code_lookup`/`term_mapping`/`value_examples` 추가
- 신규 enum: `target_class`, `recommended_strategy`, `bridge_via`
- 빈 값(`null`/`[]`) 보장 — 데이터 미적재 시에도 응답 정합 유지
- 기존 v0.5 필드는 모두 유지(forward-compatible)
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


META_VERSION = "1.0"
APP_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# 공통 키 (db, schema_name, table_name)
# ---------------------------------------------------------------------------
class TableKey(BaseModel):
    db: Optional[str] = Field(
        default=None,
        description="원천 라벨(t2s_tables.db). 미입력 시 schema+이름 동일 행 중 첫 행. "
        "ERP_SIM·RWIS 등 다중 카탈로그 구분 시 지정.",
        examples=["RWIS", "ERP_SIM"],
    )
    schema_name: str = Field(..., examples=["RWIS"])
    table_name: str = Field(..., examples=["RDF01HH_TB"])


# ---------------------------------------------------------------------------
# /meta/batch
# ---------------------------------------------------------------------------
class BatchRequest(BaseModel):
    batch_date: Optional[str] = Field(
        default=None,
        description="null=전체, 'YYYYMMDD'=해당 날짜 기준 변경·추가분",
        examples=[None, "20260420"],
    )


class BatchItem(TableKey):
    pass


# ---------------------------------------------------------------------------
# /meta/table 공통 enum
# ---------------------------------------------------------------------------
SubjectArea = Literal["agg", "raw", "code", "hist", "master", "link", "unknown"]
YN = Literal["Y", "N"]
ColumnConstraint = Literal["PK", "FK", "UNIQUE"]


# ---------------------------------------------------------------------------
# v0.6 신규 — /meta/table 보강 서브타입
# ---------------------------------------------------------------------------
class DataSourceInfo(BaseModel):
    """원천 DB 식별자·접속 메타. K-AIR-analyzer가 채움.
    데이터 미적재 시 None."""
    id: str = Field(..., description="DataSource ID (예: kwater_source_rwis)")
    dialect: Optional[str] = Field(default=None, description="postgresql/oracle/tibero/sap_hana 등")
    domain: Optional[str] = Field(default=None, description="계측·전사·분석 등 업무 도메인")
    owner: Optional[str] = Field(default=None, description="소관 부서·담당")


class LineageRef(TableKey):
    """리니지 그래프의 인접 테이블 식별자."""
    pass


class LineageBrief(BaseModel):
    """이 테이블의 상류·하류 인접 테이블 간단 요약.
    K-AIR-analyzer의 SP·DDL 파싱 결과(`analyzer_lineage_*`) 기반.
    데이터 미적재 시 두 배열 모두 빈 배열."""
    upstream: List[LineageRef] = Field(default_factory=list)
    downstream: List[LineageRef] = Field(default_factory=list)


class OntologyAnchor(BaseModel):
    """이 테이블이 5계층 온톨로지의 어느 노드와 어떻게 연결되는지.
    K-AIR 에이전트의 온톨로지 부여 결과(`ontology_graph` 엣지) 기반."""
    layer: Literal["KPI", "Measure", "Process", "Resource", "Driver"]
    name: str = Field(..., description="온톨로지 노드 이름 (예: '탁도')")
    relation: str = Field(..., description="엣지 타입 (예: SUPPORTS, MEASURES, PRODUCES)")


class CodeLookup(BaseModel):
    """FK가 명시되지 않은 코드성 컬럼이 어떤 마스터 테이블을 참조하는지 힌트.
    via=fk(직접 FK 메타) / convention(명명 규약) / term(표준용어 매핑)."""
    ref_schema_name: Optional[str] = None
    ref_table_name: Optional[str] = None
    ref_column_name: Optional[str] = None
    via: Optional[Literal["fk", "convention", "term"]] = None


class TermMapping(BaseModel):
    """Argus Catalog 합류 후 채워지는 표준용어 매핑.
    데이터 미적재 시 None."""
    standard_term: Optional[str] = None
    physical_name: Optional[str] = None
    synonyms: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# /meta/table 모델
# ---------------------------------------------------------------------------
class TableInfo(BaseModel):
    db: str
    schema_name: str
    table_name: str
    table_name_kr: Optional[str] = None
    table_comment: Optional[str] = None
    subject_area: SubjectArea = "unknown"
    table_is_active: YN = "Y"
    pk_columns: List[str] = Field(default_factory=list)

    # v0.6 신규 (빈 값 default)
    datasource: Optional[DataSourceInfo] = None
    lineage_brief: LineageBrief = Field(default_factory=LineageBrief)
    ontology_anchors: List[OntologyAnchor] = Field(default_factory=list)


class ColumnMeta(BaseModel):
    column_name: str
    column_name_kr: Optional[str] = None
    data_type: Optional[str] = None
    column_comment: Optional[str] = None
    constraints: List[ColumnConstraint] = Field(default_factory=list)
    is_null: bool = True
    column_is_active: YN = "Y"

    # v0.6 신규 (빈 값 default)
    code_lookup: Optional[CodeLookup] = None
    term_mapping: Optional[TermMapping] = None
    value_examples: List[str] = Field(default_factory=list)


class RefMeta(BaseModel):
    column_name: str
    position: int = 1
    ref_schema_name: str
    ref_table_name: str
    ref_column_name: str


class MetaTableResponse(BaseModel):
    meta_version: str = META_VERSION
    table_info: TableInfo
    columns: List[ColumnMeta]
    fk: List[RefMeta]


# ---------------------------------------------------------------------------
# /data_decision
# ---------------------------------------------------------------------------
class DecisionRequest(BaseModel):
    query: str = Field(..., min_length=1, examples=["최근 일주일간 시간별 탁도 평균"])
    include_matched_columns: bool = Field(
        default=True,
        description="False면 candidates[].matched_columns 를 채우지 않음",
    )
    column_top_m: Optional[int] = Field(
        default=None,
        ge=1,
        le=50,
        description="테이블별 컬럼 벡터 매칭 상한. 미지정 시 서버 DECISION_COLUMN_TOP_M",
    )
    table_limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=50,
        description=(
            "테이블 후보 상한(검색 top-k + 최종 candidates cap 통합). "
            "미지정 시 검색은 DECISION_VECTOR_TOPK, 최종 cap은 DECISION_TABLE_MAX(0=무제한)"
        ),
    )
    auto_resolve_entities: bool = Field(
        default=True,
        description="True면 1차 응답에 resolved_entities 자동 해소 시도 (v0.7 A안)",
    )


# v0.6 신규 enum
TargetClass = Literal["analytic", "source", "collect", "mixed", "unknown"]
TargetTop = Literal["analytic", "source", "collect", "none"]
RecommendedStrategy = Literal[
    "simple_join",
    "derived_join",
    "cte_then_join",
    "exists_filter",
    "split_execute",
]
BridgeVia = Literal["fk", "ontology", "embedding", "term", "convention"]


AnalysisStatus = Literal["complete", "degraded", "failed"]
RoleNecessity = Literal["required", "optional"]
RoleCardinality = Literal["one", "many"]
FilterOperator = Literal[
    "EQ",
    "NE",
    "IN",
    "BETWEEN",
    "GT",
    "GTE",
    "LT",
    "LTE",
    "LIKE",
    "ILIKE",
    "NOT_LIKE",
    "IS_NULL",
    "IS_NOT_NULL",
]
PlanCompleteness = Literal["complete", "partial", "degraded", "failed"]


class MeasurementRequirement(BaseModel):
    metric: Optional[str] = None
    aggregation: Optional[str] = None
    storage_type_hint: Optional[str] = None


class SchemaRoleRequirement(BaseModel):
    role: str
    necessity: RoleNecessity = "required"
    cardinality: RoleCardinality = "one"
    search_terms: List[str] = Field(default_factory=list)


class JoinRequirement(BaseModel):
    from_role: str
    to_role: str
    required: bool = True
    key_meanings: List[str] = Field(default_factory=list)


class FilterRequirement(BaseModel):
    meaning: str
    required: bool = True
    operator_hint: FilterOperator = "EQ"
    value_text: Optional[str] = None


class QuerySearchKeywords(BaseModel):
    tables: List[str] = Field(default_factory=list)
    columns: List[str] = Field(default_factory=list)


class QueryAnalysis(BaseModel):
    status: AnalysisStatus
    reason: Optional[str] = None
    fallback: Optional[Literal["question_vector"]] = None
    intent: str = ""
    entities_include: List[str] = Field(default_factory=list)
    entities_exclude: List[str] = Field(default_factory=list)
    measurement: MeasurementRequirement = Field(
        default_factory=MeasurementRequirement
    )
    schema_roles: List[SchemaRoleRequirement] = Field(default_factory=list)
    join_requirements: List[JoinRequirement] = Field(default_factory=list)
    filter_requirements: List[FilterRequirement] = Field(default_factory=list)
    search_keywords: QuerySearchKeywords = Field(
        default_factory=QuerySearchKeywords
    )


class PlannedTable(TableKey):
    table_id: Optional[int] = None
    role: str
    roles: List[str] = Field(default_factory=list)
    necessity: RoleNecessity = "required"
    required_columns: List[str] = Field(default_factory=list)
    score: float = 0.0


class PlannedJoinCondition(BaseModel):
    from_: str = Field(..., alias="from")
    to: str
    origin: Optional[str] = None
    confidence: float = 0.0

    model_config = {"populate_by_name": True}


class PlannedJoinPath(BaseModel):
    from_table: TableKey
    to_table: TableKey
    hop_count: int
    conditions: List[PlannedJoinCondition] = Field(default_factory=list)
    bridge_tables: List[TableKey] = Field(default_factory=list)
    confidence: float = 0.0


class PlannedFilter(BaseModel):
    meaning: str
    column: Optional[str] = None
    operator: FilterOperator
    value: Optional[str] = None
    resolution_status: Literal["resolved", "unresolved"] = "unresolved"
    confidence: float = 0.0


class QueryPlan(BaseModel):
    completeness: PlanCompleteness
    strategy: Optional[RecommendedStrategy] = None
    required_tables: List[PlannedTable] = Field(default_factory=list)
    bridge_tables: List[TableKey] = Field(default_factory=list)
    join_paths: List[PlannedJoinPath] = Field(default_factory=list)
    filters: List[PlannedFilter] = Field(default_factory=list)
    unresolved_requirements: List[str] = Field(default_factory=list)
    time_role: Literal["latest", "extremum", "none"] = "latest"

class MatchedColumn(BaseModel):
    """자연어 질의-컬럼 매칭 결과. 컬럼 RAG(`embedding_columns`) 활성화 후 채워짐.
    미적재 시 빈 배열.

    슬롯 매핑 정책 (테이블 측 column_name_kr/column_comment 와 동일):
      - column_name_kr : 짧은 원본 한글명 (t2s_columns.description).
      - description    : 풍부한 LLM 증강 한국어 설명 우선 + 원본 폴백
                         (t2s_columns.analyzed_description or .description).
                         v0.6.x forward-compatible 신규 필드 — 미적재 시 None.
    """
    column_name: str
    score: float = 0.0
    constraints: List[ColumnConstraint] = Field(default_factory=list)
    column_name_kr: Optional[str] = None
    data_type: Optional[str] = None
    description: Optional[str] = None
    value_examples: List[str] = Field(default_factory=list)
    format_pattern: Optional[str] = None
    unit: Optional[str] = None
    facility_code: Optional[str] = None
    system_code: Optional[str] = None
    pk_ordinal: Optional[int] = None


class DecisionCandidate(TableKey):
    score: float
    source: Literal["vector", "schema_rule", "name_rule"] = "vector"

    # v0.6 신규 (빈 값 default)
    target_class: TargetClass = "unknown"
    subject_area: SubjectArea = "unknown"
    matched_columns: List[MatchedColumn] = Field(default_factory=list)
    table_comment: Optional[str] = Field(
        default=None,
        description="원본 카탈로그 테이블 설명 (Neo4j Table.description)",
        examples=["01분 원시"],
    )
    description: Optional[str] = Field(
        default=None,
        description="LLM 증강 설명 우선, 없으면 table_comment (Neo4j analyzed_description → description)",
        examples=["시간별 탁도·유량 등 계측 fact 테이블. tagsn으로 태그 마스터와 조인."],
    )
    logical_name: Optional[str] = Field(
        default=None,
        description="승인 한글 논리명. SQL 식별자가 아님. 자연어↔물리 객체 매핑 힌트.",
    )
    table_type: Optional[Literal["Raw", "Fact", "Code", "Dimension"]] = Field(
        default=None,
        description=(
            "목록 테이블 유형. subject_area 운반체의 대응값. "
            "link/hist는 None. SQL 식별자가 아님."
        ),
    )
    default_date_column: Optional[str] = Field(
        default=None,
        description=(
            "모호한 질의의 기본 날짜 컬럼 후보. "
            "날짜 format_pattern 또는 날짜 dtype. 검수 지정 아님."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "db": "rwis",
                    "schema_name": "rwis",
                    "table_name": "fct_measure_hour",
                    "score": 0.82,
                    "source": "vector",
                    "target_class": "unknown",
                    "subject_area": "unknown",
                    "matched_columns": [],
                    "table_comment": "시간별 집계 fact",
                    "description": "시간별 계측값 fact. suj_code·tagsn으로 정수장·태그와 조인.",
                }
            ]
        }
    }


class JoinBridge(BaseModel):
    """JOIN 매개 단일 신호. fk/ontology/embedding/term 중 하나."""
    from_: str = Field(..., alias="from")
    to: str
    via: BridgeVia
    path: List[str] = Field(default_factory=list, description="ontology 매개 시 경유 노드 라벨")
    confidence: float = 0.0

    model_config = {"populate_by_name": True}


class JoinGroup(BaseModel):
    """JOIN 매개 후보 묶음. 4순위 알고리즘이 채움.
    데이터·알고리즘 미구현 시 빈 배열."""
    members: List[TableKey]
    bridge_tables: List[TableKey] = Field(default_factory=list)
    cross_db: bool = False
    recommended_strategy: Optional[RecommendedStrategy] = None
    bridges: List[JoinBridge] = Field(default_factory=list)
    group_score: float = 0.0
    score_breakdown: Dict[str, Any] = Field(default_factory=dict)
    rationale: Optional[str] = None


class ResolvedValue(BaseModel):
    code: str
    label: Optional[str] = None
    confidence: float = 1.0


class ResolvedEntity(BaseModel):
    mention: str
    entity_type: Literal[
        "facility",
        "tag",
        "code",
        "metric",
        "unit",
        "region",
        "kepco_tag",
        "kepco_metric",
        "unknown",
    ] = "unknown"
    db: Optional[str] = None
    schema_name: Optional[str] = None
    table: str
    name_column: str
    code_column: Optional[str] = None
    values: List[ResolvedValue] = Field(default_factory=list)
    source: Literal["db_probe", "value_examples"] = "db_probe"


class SuggestedProbe(BaseModel):
    purpose: str = "code_lookup"
    sql: str
    expected_columns: List[str] = Field(default_factory=list)
    maps_to_filter: Optional[Dict[str, str]] = None
    reason: Optional[str] = None


ResolutionStatus = Literal["complete", "partial", "skipped", "failed"]


class ExecutionContext(BaseModel):
    backend: str
    dialect: str
    integration: str
    catalog: str
    schema_name: str
    qualification_pattern: str
    identifier_quote: str
    require_quoted_uppercase_identifiers: bool
    source_instance_id: Optional[str] = None
    source_name: Optional[str] = None
    allowed_objects: List[str] = Field(default_factory=list)


class GlossaryRoute(BaseModel):
    mention: str
    standard_term: str
    definition: Optional[str] = None


class DecisionResponse(BaseModel):
    meta_version: str = META_VERSION
    target: TargetTop
    secondary_targets: List[Literal["analytic", "source", "collect"]] = Field(default_factory=list)
    confidence: float
    candidates: List[DecisionCandidate]
    join_groups: List[JoinGroup] = Field(default_factory=list)
    threshold_used: dict
    resolved_entities: List[ResolvedEntity] = Field(default_factory=list)
    suggested_probes: List[SuggestedProbe] = Field(default_factory=list)
    resolution_status: ResolutionStatus = "skipped"
    execution_context: Optional[ExecutionContext] = None
    query_analysis: Optional[QueryAnalysis] = None
    query_plan: Optional[QueryPlan] = None
    glossary_routes: List[GlossaryRoute] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# /t2sql
# ---------------------------------------------------------------------------
SqlStatus = Literal["generated", "failed", "validation_failed"]
SqlReasonCode = Literal[
    "NO_CANDIDATES",
    "NO_METADATA",
    "ENTITY_UNRESOLVED",
    "CROSS_DB",
    "PLAN_INCOMPLETE",
    "GUARD_REJECTED",
    "GENERATION_FAILED",
    "TIMEOUT",
    "UPSTREAM_UNAVAILABLE",
]


class T2SqlRequest(BaseModel):
    query: str = Field(..., min_length=1, examples=["화성정수장 평균 탁도"])
    include_matched_columns: bool = Field(
        default=True,
        description="False면 used_metadata.candidates[].matched_columns 를 비운다. 생성기 내부 매칭은 유지.",
    )
    column_top_m: Optional[int] = Field(default=None, ge=1, le=50)
    table_limit: Optional[int] = Field(default=None, ge=1, le=50)
    auto_resolve_entities: bool = Field(
        default=True,
        description="True면 Store value mapping을 confirm 입력에 시드. false여도 probe+confirm은 실행.",
    )
    timeout_s: Optional[int] = Field(
        default=None,
        ge=1,
        le=120,
        description=(
            "파이프라인 벽시계(초). 미지정 시 runtime total_timeout_seconds. "
            "/query_execute.timeout_s(statement timeout)와 다름."
        ),
        examples=[60],
    )

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("query must not be blank")
        return stripped


class T2SqlUsedMetadata(BaseModel):
    candidates: List[DecisionCandidate] = Field(default_factory=list)
    join_groups: List[JoinGroup] = Field(default_factory=list)
    resolved_entities: List[ResolvedEntity] = Field(default_factory=list)
    execution_context: Optional[ExecutionContext] = None
    query_analysis: Optional[QueryAnalysis] = None
    query_plan: Optional[QueryPlan] = None


class ProbeSummary(BaseModel):
    resolution_status: ResolutionStatus = "skipped"
    probes_run: int = 0
    probes_ok: int = 0
    probes_failed: int = 0
    elapsed_ms: float = 0.0
    probe_sqls: List[str] = Field(default_factory=list)
    last_error: Optional[str] = None


class T2SqlResponse(BaseModel):
    meta_version: str = META_VERSION
    generation_id: str
    sql: Optional[str] = None
    sql_status: SqlStatus
    sql_reason: Optional[str] = None
    sql_reason_code: Optional[SqlReasonCode] = None
    elapsed_ms: float = 0.0
    used_metadata: T2SqlUsedMetadata = Field(default_factory=T2SqlUsedMetadata)
    probe_summary: ProbeSummary = Field(default_factory=ProbeSummary)


# ---------------------------------------------------------------------------
# /query_execute
# ---------------------------------------------------------------------------
class QueryExecuteRequest(BaseModel):
    sql: str = Field(
        ...,
        description=(
            "SELECT/WITH/EXPLAIN(ANALYZE 제외)/SHOW 등 조회만 허용. "
            "SourceName.Schema.Table 수식 권장 (플랫폼 소스 표시명)."
        ),
        examples=[
            "SELECT * FROM `RWIS`.`SCHEMA1`.`RDISAUP_TB` LIMIT 5",
        ],
    )
    execution_context: Optional[ExecutionContext] = Field(
        default=None,
        description=(
            "/data_decision이 반환한 단일 datasource 실행 허용범위(Optional). "
            "없으면 SQL 수식의 SourceName/mindsdb catalog로 resolve. "
            "YAML default 없음."
        ),
    )
    artifact_id: Optional[str] = Field(
        default=None,
        description=(
            "폐기됨(ADR-002 Wave 2). 지정 시 410 SEMANTIC_ARTIFACT_GONE. "
            "Serving MVP는 artifact_id 없이 /query_execute를 사용."
        ),
    )
    timeout_s: Optional[int] = Field(
        default=None, ge=1, le=120,
        description="서버 statement_timeout (기본 10s, 최대 120s)",
        examples=[10],
    )
    max_rows: Optional[int] = Field(
        default=None, ge=1, le=100000,
        description="반환 최대 행수 (기본 1000, 최대 10000)",
        examples=[100],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "sql": (
                        "SELECT * FROM "
                        "`RWIS`.`SCHEMA1`.`RDISAUP_TB` "
                        "LIMIT 5"
                    ),
                    "timeout_s": 10,
                    "max_rows": 100,
                },
                {
                    "sql": (
                        "SELECT * FROM "
                        "`RWIS`.`SCHEMA1`.`RDISAUP_TB` "
                        "LIMIT 5"
                    ),
                    "execution_context": {
                        "backend": "mindsdb",
                        "dialect": "tibero",
                        "integration": "RWIS",
                        "catalog": "RWIS",
                        "schema_name": "SCHEMA1",
                        "qualification_pattern": "{catalog}.{table}",
                        "identifier_quote": "`",
                        "require_quoted_uppercase_identifiers": True,
                        "source_instance_id": "SOURCE_INSTANCE_ID",
                        "source_name": "RWIS",
                        "allowed_objects": ["RDISAUP_TB"],
                    },
                    "timeout_s": 10,
                    "max_rows": 100,
                },
            ]
        }
    }


class QueryExecuteResponse(BaseModel):
    meta_version: str = META_VERSION
    audit_id: str
    status: Literal["ok", "timeout", "db_error", "error", "rejected"]
    error: Optional[str] = None
    sql_executed: str = Field(
        description=(
            "클라이언트에 보이는 실행 SQL. SourceName.Schema.Table "
            "(플랫폼 연결 표시명). MindsDB catalog UUID는 노출하지 않는다."
        ),
    )
    columns: List[str] = Field(default_factory=list)
    rows: List[List[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    timeout_s_applied: int = 0
    max_rows_applied: int = 0
    datasource: Optional[str] = None
    query_id: Optional[str] = None
