"""Production quality runner with verified reference loading (S1-04-R4)."""

from __future__ import annotations

import pyarrow as pa

from quantking.data.quality.engine import QualityEngine
from quantking.data.quality.models import QualityRequest, QualityRunArtifact
from quantking.data.quality.references import PublishedReferenceLoader


class QualityRunner:
    """Resolve every declared reference binding before invoking the pure engine."""

    def __init__(
        self, *, engine: QualityEngine, reference_loader: PublishedReferenceLoader
    ) -> None:
        if not isinstance(engine, QualityEngine):
            raise TypeError("engine must be a QualityEngine")
        if not isinstance(reference_loader, PublishedReferenceLoader):
            raise TypeError("reference_loader must be a PublishedReferenceLoader")
        self._engine = engine
        self._reference_loader = reference_loader

    def run(self, *, request: QualityRequest, table: pa.Table) -> QualityRunArtifact:
        if not isinstance(request, QualityRequest):
            raise TypeError("request must be a QualityRequest")
        if not isinstance(table, pa.Table):
            raise TypeError("table must be a pyarrow.Table")
        references = {
            key: self._reference_loader.load(key, binding).table
            for key, binding in request.reference_data.items()
        }
        return self._engine.execute(
            request=request,
            table=table,
            reference_tables=references,
        )


__all__ = ("QualityRunner",)
