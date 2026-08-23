"""/meta/* 라우터 — batch·table·column·ref·catalog. /meta/fk alias는 제거했다."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db import get_metadata_repository
from ..schemas import (
    BatchItem,
    BatchRequest,
    CatalogResponse,
    ColumnMeta,
    MetaTableResponse,
    META_VERSION,
    RefMeta,
    TableKey,
)
from ..services import meta_postgres

router = APIRouter(tags=["meta"])


# ---------------------------------------------------------------------------
# /meta/batch
# ---------------------------------------------------------------------------
class BatchResponse(BaseModel):
    meta_version: str = META_VERSION
    items: List[BatchItem]
    total: int = 0


@router.post("/meta/batch", response_model=BatchResponse)
async def meta_batch(req: BatchRequest) -> BatchResponse:
    repository = get_metadata_repository()
    items = await meta_postgres.list_batch(repository, batch_date=req.batch_date)
    return BatchResponse(items=items, total=len(items))


# ---------------------------------------------------------------------------
# /meta/table
# ---------------------------------------------------------------------------
@router.post("/meta/table", response_model=MetaTableResponse)
async def meta_table(req: TableKey) -> MetaTableResponse:
    repository = get_metadata_repository()
    resp = await meta_postgres.get_table(
        repository,
        db=req.db,
        schema_name=req.schema_name,
        table_name=req.table_name,
    )
    if resp is None:
        raise HTTPException(status_code=404, detail="table not found")
    return resp


# ---------------------------------------------------------------------------
# /meta/column
# ---------------------------------------------------------------------------
class ColumnRequest(TableKey):
    column_name: str = Field(..., examples=["TAGSN"])


class MetaColumnResponse(BaseModel):
    meta_version: str = META_VERSION
    column: ColumnMeta


@router.post("/meta/column", response_model=MetaColumnResponse)
async def meta_column(req: ColumnRequest) -> MetaColumnResponse:
    repository = get_metadata_repository()
    col = await meta_postgres.get_column(
        repository,
        db=req.db,
        schema_name=req.schema_name,
        table_name=req.table_name,
        column_name=req.column_name,
    )
    if col is None:
        raise HTTPException(status_code=404, detail="column not found")
    return MetaColumnResponse(column=col)


# ---------------------------------------------------------------------------
# /meta/ref
# ---------------------------------------------------------------------------
class MetaRefResponse(BaseModel):
    meta_version: str = META_VERSION
    fk: List[RefMeta]


@router.post("/meta/ref", response_model=MetaRefResponse)
async def meta_ref(req: TableKey) -> MetaRefResponse:
    repository = get_metadata_repository()
    fks = await meta_postgres.get_refs(
        repository,
        db=req.db,
        schema_name=req.schema_name,
        table_name=req.table_name,
    )
    return MetaRefResponse(fk=fks)


# ---------------------------------------------------------------------------
# /meta/catalog
# ---------------------------------------------------------------------------
@router.post(
    "/meta/catalog",
    response_model=CatalogResponse,
    response_model_exclude_none=True,
)
async def meta_catalog() -> CatalogResponse:
    repository = get_metadata_repository()
    return await meta_postgres.get_serving_catalog(repository)
