"""KAIR Neo4j Physical Layer 소비 어댑터.

목적:
- robo/KAIR 간 관계명 혼재(대소문자·legacy)를 소비 계층에서 흡수
- 테이블 식별키를 결정적으로 정규화(datasource+schema+name)
- db 라벨 폴백 체인(Schema.db -> DataSource.engine -> fallback)을 공통화
"""
from __future__ import annotations

from typing import Optional

# 관계명 union (KAIR/robo 동시 호환)
REL_TABLE_SCHEMA = "belongsTo|HAS_TABLE"
REL_SCHEMA_DATASOURCE = "HAS_SCHEMA"
REL_TABLE_COLUMN = "hasColumn|HAS_COLUMN"
REL_COLUMN_FK = "fkTo|FK_TO_COLUMN"
REL_TABLE_FK = "fkToTable|FK_TO_TABLE"


def _norm(v: Optional[str]) -> str:
    return str(v or "").strip()


def _norm_lower(v: Optional[str]) -> str:
    return _norm(v).lower()


def table_lookup_key(*, datasource: Optional[str], schema: Optional[str], name: Optional[str]) -> str:
    """결정적 테이블 조회 키.

    형식:
    - datasource가 있으면 `<datasource>|<schema>.<name>`
    - 없으면 `<schema>.<name>`
    - schema도 없으면 `<name>`
    """
    ds = _norm_lower(datasource)
    sch = _norm_lower(schema)
    nm = _norm_lower(name)
    if not nm:
        return ""
    base = f"{sch}.{nm}" if sch else nm
    return f"{ds}|{base}" if ds else base


def coalesce_db_label(*, schema_db: Optional[str], datasource_engine: Optional[str], fallback: str) -> str:
    """R-3 db 라벨 폴백 체인."""
    return _norm(schema_db) or _norm(datasource_engine) or _norm(fallback)
