"""계약 Pydantic 모델 (v0.5 → v0.6 확장).

v0.6 변경 요약:
- 응답에 `meta_version` 동봉
- /data_decision: candidate별 `target_class`/`subject_area`/`matched_columns`,
  최상위 `join_groups` 추가
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
    source_name: Optional[str] = Field(
        default=None,
        description="데이터소스 연결명 (플랫폼 SourceName).",
        examples=["rwis_mart_view"],
    )
    db: Optional[str] = Field(
        default=None,
        description="원천 database 이름. datasources.database_name, 없으면 t2s_tables.db.",
        examples=["rwis"],
    )
    engine: Optional[str] = Field(
        default=None,
        description="원천 DB 엔진. postgresql/oracle/tibero 등.",
        examples=["postgresql"],
    )
    schema_name: str = Field(..., examples=["RWIS"])
    table_name: str = Field(..., examples=["RDF01HH_TB"])


# ---------------------------------------------------------------------------
# /meta/batch · 공통 enum
# ---------------------------------------------------------------------------
SubjectArea = Literal["agg", "raw", "code", "hist", "master", "link", "unknown"]
YN = Literal["Y", "N"]
ColumnConstraint = Literal["PK", "FK", "UNIQUE"]
ListTableType = Literal["Raw", "Fact", "Code", "Dimension"]


class BatchRequest(BaseModel):
    batch_date: Optional[str] = Field(
        default=None,
        description="null=전체, 'YYYYMMDD'=해당 날짜 기준 변경·추가분",
        examples=[None, "20260420"],
    )


class BatchItem(TableKey):
    table_name_kr: Optional[str] = Field(
        default=None,
        description="승인 한글 논리명. SQL 식별자가 아님.",
    )
    table_comment: Optional[str] = Field(
        default=None,
        description="원본 카탈로그 테이블 설명",
    )
    description: Optional[str] = Field(
        default=None,
        description="분석 설명 우선, 없으면 table_comment",
    )
    subject_area: SubjectArea = "unknown"


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
    table_name_kr: Optional[str] = Field(
        default=None,
        description="승인 한글 논리명. SQL 식별자가 아님.",
    )
    table_comment: Optional[str] = Field(
        default=None,
        description="원본 카탈로그 테이블 설명",
    )
    description: Optional[str] = Field(
        default=None,
        description="분석 설명 우선, 없으면 table_comment",
    )
    subject_area: SubjectArea = "unknown"
    table_type: Optional[ListTableType] = Field(
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
    table_is_active: YN = "Y"
    pk_columns: List[str] = Field(default_factory=list)

    # v0.6 신규 (빈 값 default)
    row_count: Optional[int] = Field(
        default=None,
        description="메타데이터 저장소에 기록된 테이블의 전체 행 수(Row Count).",
    )
    datasource: Optional[DataSourceInfo] = None
    lineage_brief: LineageBrief = Field(default_factory=LineageBrief)
    ontology_anchors: List[OntologyAnchor] = Field(default_factory=list)


class ColumnMeta(BaseModel):
    column_name: str
    column_name_kr: Optional[str] = Field(
        default=None,
        description="짧은 한글 논리명. 설명 장문을 넣지 않는다.",
    )
    data_type: Optional[str] = None
    column_comment: Optional[str] = Field(
        default=None,
        description="원본 카탈로그 컬럼 설명",
    )
    description: Optional[str] = Field(
        default=None,
        description="분석 설명 우선, 없으면 column_comment",
    )
    constraints: List[ColumnConstraint] = Field(default_factory=list)
    is_null: bool = True
    column_is_active: YN = "Y"
    format_pattern: Optional[str] = None
    unit: Optional[str] = None
    facility_code: Optional[str] = None
    system_code: Optional[str] = None
    pk_ordinal: Optional[int] = None
    has_code: YN = Field(
        default="N",
        description="승인 값사전의 code_column이면 Y. 코드 표의 모든 컬럼이 Y는 아니다.",
    )

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
# /meta/catalog — Serving 정본 (표·컬럼 설명·FK 포함)
# ---------------------------------------------------------------------------
class CatalogColumnReference(BaseModel):
    schema_name: str
    table_name: str
    column_name: str
    constraint_name: Optional[str] = None
    position: int = 1


class CatalogColumn(BaseModel):
    column_name: str
    data_type: Optional[str] = None
    nullable: bool = True
    primary_key: bool = False
    comment: Optional[str] = Field(
        default=None,
        description="원본 DDL/카탈로그 컬럼 설명",
    )
    description: Optional[str] = Field(
        default=None,
        description="검수·서빙 설명. 분석 설명 우선, 없으면 comment",
    )
    references: Optional[CatalogColumnReference] = None
    referenced_by: Optional[List[CatalogColumnReference]] = None


class CatalogTable(BaseModel):
    table_name: str
    logical_name: Optional[str] = Field(
        default=None,
        description="승인 한글 논리명. SQL 식별자가 아님.",
    )
    comment: Optional[str] = Field(
        default=None,
        description="원본 DDL/카탈로그 테이블 설명",
    )
    description: Optional[str] = Field(
        default=None,
        description="검수·서빙 설명. 분석 설명 우선, 없으면 comment",
    )
    row_count: Optional[int] = Field(
        default=None,
        description="메타데이터 저장소에 기록된 테이블 행 수.",
    )
    subject_area: SubjectArea = "unknown"
    columns: List[CatalogColumn] = Field(default_factory=list)


class CatalogSource(BaseModel):
    source_name: str
    engine: str
    source_schema: Optional[str] = None
    registered_at: str
    tables: List[CatalogTable] = Field(default_factory=list)


class CatalogResponse(BaseModel):
    meta_version: str = META_VERSION
    serving_status: Literal["active", "inactive"]
    sources: List[CatalogSource] = Field(default_factory=list)


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
        description=(
            "테이블별 matched_columns 상한. 질문 분해·계획에 관련된 컬럼만 남긴 뒤 자른다. "
            "미지정 시 서버 decision.column_top_m"
        ),
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
    "NOT_IN",
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


class SchemaRoleRequirement(BaseModel):
    role: str
    necessity: RoleNecessity = "required"
    cardinality: RoleCardinality = "one"
    search_terms: List[str] = Field(default_factory=list)


class FilterRequirement(BaseModel):
    meaning: str
    required: bool = True
    operator_hint: FilterOperator = "EQ"
    value_text: Optional[str] = None


class QueryAnalysis(BaseModel):
    status: AnalysisStatus
    query: str = ""
    reason: Optional[str] = None
    measurement: MeasurementRequirement = Field(
        default_factory=MeasurementRequirement
    )
    schema_roles: List[SchemaRoleRequirement] = Field(default_factory=list)
    filter_requirements: List[FilterRequirement] = Field(default_factory=list)
    goal: str = ""
    procedure: str = ""
    procedure_why: str = ""
    metric: str = ""
    target: str = ""
    period: str = ""
    primary_outputs: List[str] = Field(default_factory=list)
    answer_must_include: List[str] = Field(default_factory=list)
    meaning_status: str = ""


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


class PlanAggregationTag(BaseModel):
    """tagsn 확정 뒤에만 채운다. unit은 unit_desc 표시값."""

    tagsn: str
    data_process: Optional[str] = None
    apply: Optional[str] = None
    unit: Optional[str] = None
    source_column: Optional[str] = None


class PlanAggregation(BaseModel):
    """Optional aggregation contract. Missing store facts stay null. LLM must not fill."""

    function: Optional[str] = None
    value_column: Optional[str] = None
    weighted: Optional[bool] = None
    weight_column: Optional[str] = None
    null_outlier_policy: Optional[str] = None
    time_scope: Optional[str] = None
    data_as_of: Optional[str] = None
    tag_combine: Optional[str] = None
    tags: List[PlanAggregationTag] = Field(default_factory=list)


class CandidateEvidence(BaseModel):
    """값매핑·카탈로그·팩트 후보의 적중·선정 근거. 사유는 비울 수 없다."""

    kind: Literal["value_mapping", "catalog", "fact"]
    mention: Optional[str] = None
    match_type: Optional[str] = None
    matched_field: Optional[str] = None
    score: Optional[float] = None
    selected: bool
    reason: str = Field(..., min_length=1)
    table_id: Optional[int] = None
    table_name: Optional[str] = None
    schema_name: Optional[str] = None
    code_value: Optional[str] = None
    column_fqn: Optional[str] = None

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("candidate evidence reason must not be blank")
        return text


class QueryPlan(BaseModel):
    completeness: PlanCompleteness
    strategy: Optional[RecommendedStrategy] = None
    required_tables: List[PlannedTable] = Field(default_factory=list)
    bridge_tables: List[TableKey] = Field(default_factory=list)
    join_paths: List[PlannedJoinPath] = Field(default_factory=list)
    filters: List[PlannedFilter] = Field(default_factory=list)
    unresolved_requirements: List[str] = Field(default_factory=list)
    time_role: Literal["latest", "extremum", "none"] = "none"
    answer_axis: List[str] = Field(default_factory=list)
    aggregation: Optional[PlanAggregation] = None
    candidate_evidence: List[CandidateEvidence] = Field(default_factory=list)
    universal_plan: Optional[UniversalServingPlan] = None

class MatchedColumn(BaseModel):
    """자연어 질의-컬럼 매칭 결과. 컬럼 RAG(`embedding_columns`) 활성화 후 채워짐.
    미적재 시 빈 배열.

    슬롯 매핑 정책:
      - column_name_kr : 짧은 한글 논리명
        (metadata.column_name_kr / logical_name / 승인 표준화 제안명).
        설명 장문을 넣지 않는다.
      - description    : 분석 설명 우선, 없으면 원본 설명
                         (t2s_columns.analyzed_description or .description).
    """
    column_name: str
    constraints: List[ColumnConstraint] = Field(default_factory=list)
    column_name_kr: Optional[str] = None
    data_type: Optional[str] = None
    column_description: Optional[str] = None
    value_examples: List[str] = Field(default_factory=list)
    format_pattern: Optional[str] = None
    unit: Optional[str] = None
    facility_code: Optional[str] = None
    system_code: Optional[str] = None
    pk_ordinal: Optional[int] = None
    has_code: YN = Field(
        default="N",
        description="승인 값사전의 code_column이면 Y. 코드 표의 모든 컬럼이 Y는 아니다.",
    )


class DecisionCandidate(TableKey):
    source: Literal["vector", "schema_rule", "name_rule"] = "vector"

    # v0.6 신규 (빈 값 default)
    target_class: TargetClass = "unknown"
    subject_area: SubjectArea = "unknown"
    table_name_kr: Optional[str] = Field(
        default=None,
        description="승인 한글 논리명. SQL 식별자가 아님. column_name_kr과 같은 층.",
        examples=["일 DATA"],
    )
    table_description: Optional[str] = Field(
        default=None,
        description="분석 설명 우선, 없으면 원본 카탈로그 설명 (analyzed_description → description)",
        examples=["시간별 탁도·유량 등 계측 fact 테이블. tagsn으로 태그 마스터와 조인."],
    )
    row_count: Optional[int] = Field(
        default=None,
        description="메타데이터 저장소에 기록된 테이블 전체 행 수.",
    )
    table_type: Optional[ListTableType] = Field(
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
    matched_columns: List[MatchedColumn] = Field(default_factory=list)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "source_name": "rwis_mart_view",
                    "db": "rwis_mart_view",
                    "engine": "postgresql",
                    "schema_name": "rwis",
                    "table_name": "fct_measure_hour",
                    "source": "vector",
                    "target_class": "unknown",
                    "subject_area": "unknown",
                    "table_name_kr": "시간 DATA",
                    "table_description": "시간별 계측값 fact. suj_code·tagsn으로 정수장·태그와 조인.",
                    "matched_columns": [],
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
class UniversalEntity(BaseModel):
    """범용 엔티티 (시설, 댐, 관측소, 관로, 블록 등)"""
    entity_type: str = Field(..., description="엔티티 유형 (facility, dam, observatory, station, pipe, block 등)")
    entity_id: Optional[str] = Field(default=None, description="엔티티 식별 코드")
    entity_name: Optional[str] = Field(default=None, description="엔티티 명칭")
    table_name: Optional[str] = Field(default=None, description="엔티티 소속 기준정보 테이블")
    join_key: Optional[str] = Field(default=None, description="JOIN 키 컬럼")
    confidence: float = 1.0


class UniversalMetric(BaseModel):
    """범용 수치 지표 (수위, 유입량, 방류량, 탁도, 압력, 유량 등)"""
    metric_name: str = Field(..., description="지표명")
    column_name: str = Field(..., description="물리 수치 컬럼명")
    table_name: Optional[str] = Field(default=None, description="계측 팩트 테이블명")
    unit: Optional[str] = Field(default=None, description="단위")
    aggregation: str = Field(default="AVG", description="집계 함수 (AVG, SUM, MAX, MIN 등)")


class UniversalFilter(BaseModel):
    """범용 차원/상태 필터"""
    column_name: str = Field(..., description="필터 컬럼명")
    operator: str = Field(default="=", description="연산자")
    value: Any = Field(..., description="필터 값")
    table_name: Optional[str] = Field(default=None, description="대상 테이블")


class UniversalServingPlan(BaseModel):
    """범용 엔티티-메트릭 서빙 플랜 (v2 서빙 계약)"""
    system_type: Optional[str] = Field(default="AUTO", description="데이터 소스 도메인 (RWIS, HDAPS, GIOS 등)")
    entities: List[UniversalEntity] = Field(default_factory=list)
    metrics: List[UniversalMetric] = Field(default_factory=list)
    filters: List[UniversalFilter] = Field(default_factory=list)
    time_range: Optional[Dict[str, Any]] = None
    sql_strategy: Optional[str] = None
    # 하위 호환성 투영 레이어 (RWIS 클라이언트 보장)
    tags: List[str] = Field(default_factory=list, description="RWIS 하위 호환 태그 ID 목록")
    tagsn: List[str] = Field(default_factory=list, description="RWIS 하위 호환 태그 한글명 목록")


class ResolvedEntity(BaseModel):
    mention: str
    entity_type: Literal[
        "facility",
        "tag",
        "dam",
        "observatory",
        "station",
        "pipe",
        "block",
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
    matched_mention: Optional[str] = None
    values: List[ResolvedValue] = Field(default_factory=list)
    source: Literal["db_probe", "value_examples", "glossary"] = "db_probe"


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
    source_instance_id: Optional[str] = Field(default=None, exclude=True)
    source_name: Optional[str] = None
    allowed_objects: List[str] = Field(default_factory=list)


class GlossaryRoute(BaseModel):
    mention: str
    standard_term: str
    definition: Optional[str] = None


class DecisionResponse(BaseModel):
    meta_version: str = META_VERSION
    target: TargetTop
    candidates: List[DecisionCandidate]
    join_groups: List[JoinGroup] = Field(default_factory=list)
    threshold_used: dict
    resolved_entities: List[ResolvedEntity] = Field(default_factory=list)
    resolution_status: ResolutionStatus = "skipped"
    execution_context: Optional[ExecutionContext] = None
    query_analysis: Optional[QueryAnalysis] = None
    query_plan: Optional[QueryPlan] = None
    glossary_routes: List[GlossaryRoute] = Field(default_factory=list)
    universal_plan: Optional[UniversalServingPlan] = None


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
    table_limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=50,
        description=(
            "계획에 올릴 표 수 상한. 미지정 시 runtime decision.table_top_k."
        ),
    )
    auto_resolve_entities: bool = Field(
        default=True,
        description=(
            "include_resolved_entities와 동일. True면 응답 "
            "used_metadata.resolved_entities를 채운다. false여도 계획 필터·probe·confirm은 실행."
        ),
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
    candidate_evidence: List[CandidateEvidence] = Field(default_factory=list)
    universal_plan: Optional[UniversalServingPlan] = None


class ProbeSummary(BaseModel):
    resolution_status: ResolutionStatus = "skipped"
    probes_run: int = 0
    probes_ok: int = 0
    probes_failed: int = 0
    elapsed_ms: float = 0.0
    probe_sqls: List[str] = Field(default_factory=list)
    last_error: Optional[str] = None


class PipelineStage(BaseModel):
    name: str
    elapsed_ms: float = 0.0
    candidate_count: Optional[int] = None
    model: Optional[str] = None
    prompt: Optional[str] = None
    snapshot_id: Optional[str] = None
    validation_error: Optional[str] = None
    detail: Optional[str] = None


class T2SqlResponse(BaseModel):
    meta_version: str = META_VERSION
    generation_id: str
    sql: Optional[str] = None
    draft_sql: Optional[str] = None
    sql_status: SqlStatus
    sql_reason: Optional[str] = None
    sql_reason_code: Optional[SqlReasonCode] = None
    elapsed_ms: float = 0.0
    used_metadata: T2SqlUsedMetadata = Field(default_factory=T2SqlUsedMetadata)
    probe_summary: ProbeSummary = Field(default_factory=ProbeSummary)
    pipeline_stages: List[PipelineStage] = Field(default_factory=list)
    metadata_snapshot_id: Optional[str] = None
    sql_fingerprint: Optional[str] = None


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
        default=None, ge=1, le=600,
        description=(
            "서버 statement_timeout 요청값. "
            "기본·상한은 execution YAML과 EXEC_DEFAULT_TIMEOUT_S / "
            "EXEC_MAX_TIMEOUT_S. 요청이 상한을 넘으면 상한으로 자른다."
        ),
        examples=[60],
    )
    max_rows: Optional[int] = Field(
        default=None, ge=1, le=100000,
        description="반환 최대 행수 (기본 1000, 최대 10000)",
        examples=[100],
    )
    fingerprint: Optional[str] = Field(
        default=None,
        description=(
            "/t2sql sql_fingerprint. 있으면 실행 SQL과 다시 비교한다. "
            "없어도 기존 읽기 SQL 가드 후 실행한다. 필수가 아니다."
        ),
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
