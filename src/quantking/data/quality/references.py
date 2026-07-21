"""Verified reference data loading for S1-04 quality rules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa

from quantking.common.ids import EntityId
from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory
from quantking.data.adapters.warehouse.query import CanonicalQueryAdapter
from quantking.data.models import DatasetKind, DatasetVersionStatus
from quantking.data.quality.models import ReferenceDataBinding


class ReferenceDataBindingError(Exception):
    """Raised when a reference binding cannot be proven against a published version."""


@dataclass(frozen=True, slots=True)
class LoadedReferenceTable:
    key: str
    dataset: DatasetKind
    version_id: EntityId
    content_sha256: str
    table: pa.Table


class PublishedReferenceLoader:
    """Load reference tables through the strict published-version query path."""

    def __init__(self, *, data_root: Path, uow_factory: DataSqliteUnitOfWorkFactory) -> None:
        self._query = CanonicalQueryAdapter(data_root=data_root, uow_factory=uow_factory)
        self._uow_factory = uow_factory

    def load(self, key: str, binding: ReferenceDataBinding) -> LoadedReferenceTable:
        if not isinstance(key, str) or not key.strip():
            raise ReferenceDataBindingError("reference key must be non-empty text")
        if not isinstance(binding, ReferenceDataBinding):
            raise TypeError("binding must be a ReferenceDataBinding")
        with self._uow_factory() as uow:
            version = uow.data.get_version(str(binding.version_id))
            if version.status is not DatasetVersionStatus.PUBLISHED:
                raise ReferenceDataBindingError("reference version must be PUBLISHED")
            if version.dataset is not binding.dataset:
                raise ReferenceDataBindingError("reference dataset does not match binding")
            if version.content_sha256 != binding.content_sha256:
                raise ReferenceDataBindingError("reference content SHA does not match binding")
        table = self._query.query_by_id(
            version_id=str(binding.version_id), dataset=binding.dataset.value, fields=None
        )
        return LoadedReferenceTable(
            key=key,
            dataset=binding.dataset,
            version_id=binding.version_id,
            content_sha256=binding.content_sha256,
            table=table,
        )


__all__ = ("LoadedReferenceTable", "PublishedReferenceLoader", "ReferenceDataBindingError")
