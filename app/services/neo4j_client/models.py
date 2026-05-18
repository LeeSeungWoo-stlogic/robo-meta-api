"""
데이터 모델 — K-AIR robo-data-text2sql/app/react/tools/build_sql_context_parts/models.py 기반.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 벡터 검색 후보 모델 (K-AIR 원본 구조 유지)
# ---------------------------------------------------------------------------

@dataclass
class TableCandidate:
    schema: str
    name: str
    description: str
    analyzed_description: str = ""
    score: float = 0.0

    @property
    def fqn(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


@dataclass
class ColumnCandidate:
    table_schema: str
    table_name: str
    name: str
    dtype: str
    description: str
    score: float = 0.0

    @property
    def table_fqn(self) -> str:
        return f"{self.table_schema}.{self.table_name}" if self.table_schema else self.table_name

    @property
    def column_fqn(self) -> str:
        return f"{self.table_fqn}.{self.name}" if self.table_fqn else self.name


# ---------------------------------------------------------------------------
# HyDE 모델 (K-AIR robo-data-text2sql/app/react/generators/hyde_schema_generator.py 기반)
# ---------------------------------------------------------------------------

class HydeEntities(BaseModel):
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class HydeMeasurement(BaseModel):
    aggregation: str = ""
    metric_meaning: str = ""
    storage_type_hint: str = ""


class HydeJoinFilterHints(BaseModel):
    join_keys: list[str] = Field(default_factory=list)
    filter_column_meanings: list[str] = Field(default_factory=list)
    needs_time_range: Optional[bool] = None


class HydeSearchKeywords(BaseModel):
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)


class HydeSchemaOut(BaseModel):
    intent: str = ""
    entities: HydeEntities = Field(default_factory=HydeEntities)
    measurement: HydeMeasurement = Field(default_factory=HydeMeasurement)
    schema_roles: list[str] = Field(default_factory=list)
    join_filter_hints: HydeJoinFilterHints = Field(default_factory=HydeJoinFilterHints)
    search_keywords: HydeSearchKeywords = Field(default_factory=HydeSearchKeywords)


# ---------------------------------------------------------------------------
# API 요청/응답 모델
# ---------------------------------------------------------------------------

class ResolveQuestionRequest(BaseModel):
    question: str
    schema_filter: Optional[str] = None
    top_k: int = 5
    enable_db_probe: bool = True


class ResolveQuestionResponse(BaseModel):
    question: str
    hyde: Optional[Dict[str, Any]] = None
    selected_tables: List[Dict[str, Any]] = Field(default_factory=list)
    table_schemas: List[Dict[str, Any]] = Field(default_factory=list)
    fk_relationships: List[Dict[str, Any]] = Field(default_factory=list)
    db_probe_results: Optional[Dict[str, Any]] = None
    debug: Dict[str, Any] = Field(default_factory=dict)
