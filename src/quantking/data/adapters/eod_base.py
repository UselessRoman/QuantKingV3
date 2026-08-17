"""Strict exact-version resolver used only while freezing an EOD command."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from quantking.data.adapters.persistence.sqlite import DataSqliteUnitOfWorkFactory
from quantking.data.adapters.warehouse.query import CanonicalQueryAdapter
from quantking.data.eod.contracts import EodBaseVersion
from quantking.data.models import DatasetKind, DatasetVersionStatus, QualityRunStatus


class PublishedEodBaseResolver:
    """Resolve one exact PUBLISHED version and freeze verified credentials."""

    def __init__(
        self,
        *,
        data_root: Path,
        uow_factory: DataSqliteUnitOfWorkFactory,
    ) -> None:
        self._uow = uow_factory
        self._query = CanonicalQueryAdapter(data_root=data_root, uow_factory=uow_factory)

    def resolve(
        self,
        *,
        dataset: str,
        version_id: str,
        scope: tuple[str, ...],
    ) -> EodBaseVersion:
        kind = DatasetKind(dataset)
        try:
            with self._uow() as uow:
                version = uow.data.get_version(version_id)
                quality = (
                    None
                    if version.quality_run_id is None
                    else uow.data.get_quality_run(str(version.quality_run_id))
                )
                uow.rollback()
            verified = self._query.verify_manifest_by_id(
                version_id=version_id,
                dataset=dataset,
            )
            table = self._query.query_by_id(version_id=version_id, dataset=dataset)
        except Exception as error:
            raise ValueError("base version could not be strictly resolved") from error
        if (
            version.status is not DatasetVersionStatus.PUBLISHED
            or version.dataset is not kind
            or version.content_sha256 is None
            or version.manifest_sha256 is None
            or verified.manifest_sha != version.manifest_sha256
            or verified.manifest.get("content_sha256") != version.content_sha256
            or quality is None
            or quality.status is not QualityRunStatus.PASSED
            or quality.version_id != version.version_id
            or quality.rule_version != version.rule_version
            or quality.schema_version != version.schema_version
            or quality.content_sha256 != version.content_sha256
        ):
            raise ValueError("base version credentials do not match")
        date_field = "effective_from" if kind is DatasetKind.SECURITY_MASTER else "trade_date"
        scope_field = "exchange" if kind is DatasetKind.EXCHANGE_CALENDAR else "code"
        if date_field not in table.column_names or scope_field not in table.column_names:
            raise ValueError("base version coverage fields are unavailable")
        dates: dict[str, list[date]] = {item: [] for item in scope}
        for row in table.select([scope_field, date_field]).to_pylist():
            item_scope = row[scope_field]
            item_date = row[date_field]
            if item_scope in dates and isinstance(item_date, date):
                dates[item_scope].append(item_date)
        if any(not values for values in dates.values()):
            raise ValueError("base version does not cover every configured scope")

        def runs(values: list[date]) -> list[tuple[date, date]]:
            ordered = sorted(values)
            segments: list[tuple[date, date]] = []
            start = previous = ordered[0]
            for current in ordered[1:]:
                if current != previous + timedelta(days=1):
                    segments.append((start, previous))
                    start = current
                previous = current
            segments.append((start, previous))
            return segments

        # Emit each security's real contiguous coverage segments (with internal
        # gaps preserved as separate windows) instead of collapsing to a global
        # (min, max) pair that would fabricate continuous coverage.
        coverage: list[tuple[str, str, str]] = []
        for item in sorted(dates):
            for start, end in runs(dates[item]):
                coverage.append((item, start.isoformat(), end.isoformat()))
        return EodBaseVersion(
            version_id=version_id,
            dataset=dataset,
            schema_version=version.schema_version,
            content_sha256=version.content_sha256,
            manifest_sha256=version.manifest_sha256,
            coverage=tuple(coverage),
        )


__all__ = ["PublishedEodBaseResolver"]
