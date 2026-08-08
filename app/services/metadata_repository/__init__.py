"""Postgres metadata repository (mixin package).

`PostgresMetadataRepository` remains importable from
`app.services.metadata_repository` with the same public methods.
"""
from __future__ import annotations

from ._base import MetadataRepositoryBase, _vector_literal
from ._catalog import CatalogMixin
from ._execution import ExecutionSourceMixin
from ._joins import JoinGraphMixin
from ._search import SearchMixin

__all__ = ["PostgresMetadataRepository", "_vector_literal"]


class PostgresMetadataRepository(
    ExecutionSourceMixin,
    SearchMixin,
    JoinGraphMixin,
    CatalogMixin,
    MetadataRepositoryBase,
):
    """Serving-layer metadata access used by decision / meta / query paths."""
