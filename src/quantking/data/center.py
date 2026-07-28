"""Framework-neutral read models and query service for the Stage 1 data center."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol, Self

from quantking.common.ids import EntityId, parse_entity_id
from quantking.common.time import Clock, TradingDate, utc_now
from quantking.data.adapters.warehouse.errors import QueryContractError
from quantking.data.assessment.matrix import default_source_matrix
from quantking.data.models import (
    BatchFailureInfo,
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    DataSource,
    DifferenceReport,
    IngestionBatch,
    IngestionBatchStatus,
    QualityRun,
    QualityRunStatus,
    SourceOverride,
    SourceOverrideStatus,
)
from quantking.jobs.models import FailureInfo, JobRun, JobStatus

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
MAX_PREVIEW_LIMIT = 200


class InvalidDataCenterQueryError(ValueError):
    """Stable caller error for an invalid filter or opaque cursor."""


@dataclass(frozen=True, slots=True)
class DataCenterPage:
    items: tuple[Mapping[str, object], ...]
    next_cursor: str | None


class DataRepositoryPort(Protocol):
    def get_batch(self, batch_id: str) -> IngestionBatch: ...
    def get_version(self, version_id: str) -> DatasetVersion: ...
    def get_quality_run(self, run_id: str) -> QualityRun: ...
    def get_difference_report(self, report_id: str) -> DifferenceReport: ...
    def get_override(self, override_id: str) -> SourceOverride: ...
    def get_override_application_binding(self, override_id: str) -> OverrideBinding: ...
    def find_last_override_application_attempt(self, override_id: str) -> OverrideAttempt: ...

    def list_batches_page(
        self,
        *,
        dataset: DatasetKind | None,
        source: DataSource | None,
        status: IngestionBatchStatus | None,
        started_from: str | None,
        started_to: str | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[IngestionBatch]: ...

    def list_versions_page(
        self,
        *,
        dataset: DatasetKind | None,
        status: DatasetVersionStatus | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[DatasetVersion]: ...

    def list_quality_runs_page(
        self,
        *,
        version_id: str | None,
        status: QualityRunStatus | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[QualityRun]: ...

    def list_difference_reports_page(
        self,
        *,
        version_id: str | None,
        primary_source: DataSource | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[DifferenceReport]: ...

    def list_overrides_page(
        self,
        *,
        dataset: DatasetKind | None,
        status: SourceOverrideStatus | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[SourceOverride]: ...


class DataUnitOfWork(Protocol):
    @property
    def data(self) -> DataRepositoryPort: ...

    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


class DataUnitOfWorkFactory(Protocol):
    def __call__(self) -> DataUnitOfWork: ...


class CanonicalPreviewPort(Protocol):
    def query_by_id(
        self,
        *,
        version_id: str,
        dataset: str,
        fields: Sequence[str] | None = None,
        codes: Sequence[str] | None = None,
        start_date: TradingDate | None = None,
        end_date: TradingDate | None = None,
        after_sort_key: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> CanonicalTable: ...


class CanonicalTable(Protocol):
    def to_pylist(self) -> list[dict[str, object]]: ...


class VerifiedDifference(Protocol):
    report_sha256: str
    row_differences: list[dict[str, object]]
    created_at: Any


class OverrideBinding(Protocol):
    status: str
    child_version_id: EntityId
    report_sha256: str | None


class OverrideAttempt(Protocol):
    status: str
    child_version_id: EntityId
    failure_message: str | None


class DifferenceVerifierPort(Protocol):
    def verify(self, *, report_id: EntityId) -> VerifiedDifference: ...


class JobListPort(Protocol):
    def get_job(self, job_id: str) -> JobRun: ...
    def list_jobs_page(
        self,
        *,
        job_type: str | None,
        status: JobStatus | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[JobRun]: ...


def _cursor(kind: str, timestamp: str, stable_id: str, values: Sequence[str] = ()) -> str:
    raw = json.dumps(
        {"v": 1, "k": kind, "t": timestamp, "i": stable_id, "s": list(values)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _parse_cursor(value: str | None, kind: str) -> tuple[str, str, tuple[str, ...]] | None:
    if value is None:
        return None
    if not value or len(value) > 1024:
        raise InvalidDataCenterQueryError("cursor is invalid")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidDataCenterQueryError("cursor is invalid") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"i", "k", "s", "t", "v"}
        or payload["v"] != 1
        or payload["k"] != kind
        or not isinstance(payload["t"], str)
        or not isinstance(payload["i"], str)
        or not payload["t"]
        or not payload["i"]
        or not isinstance(payload["s"], list)
        or any(not isinstance(item, str) or not item for item in payload["s"])
    ):
        raise InvalidDataCenterQueryError("cursor is invalid")
    try:
        datetime.fromisoformat(payload["t"].replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidDataCenterQueryError("cursor is invalid") from error
    return payload["t"], payload["i"], tuple(payload["s"])


def validate_cursor(value: str | None, kind: str) -> None:
    """Validate an opaque cursor at a transport boundary without returning internals."""
    _parse_cursor(value, kind)


def _failure(value: BatchFailureInfo | FailureInfo | None) -> Mapping[str, object] | None:
    if value is None:
        return None
    return {
        "code": str(getattr(value.error_type, "value", value.error_type)),
        "message": str(value.message),
        "retryable": bool(value.retryable),
    }


def _batch(item: IngestionBatch) -> Mapping[str, object]:
    return {
        "batch_id": str(item.batch_id),
        "source": item.source.value,
        "dataset": item.dataset.value,
        "status": item.status.value,
        "started_at": item.started_at.to_iso(),
        "ended_at": item.ended_at.to_iso() if item.ended_at else None,
        "row_count": item.row_count,
        "security_count": item.security_count,
        "job_id": item.job_run_id,
        "failure": _failure(item.failure),
    }


def _version(item: DatasetVersion) -> Mapping[str, object]:
    return {
        "version_id": str(item.version_id),
        "dataset": item.dataset.value,
        "status": item.status.value,
        "schema_version": item.schema_version,
        "rule_version": item.rule_version,
        "parent_version_id": str(item.parent_version_id) if item.parent_version_id else None,
        "input_batch_ids": [str(value) for value in item.input_batch_ids],
        "created_at": item.created_at.to_iso(),
        "published_at": item.published_at.to_iso() if item.published_at else None,
        "row_count": item.row_count,
        "security_count": item.security_count,
        "coverage_start": str(item.coverage_start) if item.coverage_start else None,
        "coverage_end": str(item.coverage_end) if item.coverage_end else None,
        "coverage": dict(item.coverage_summary),
        "quality_run_id": str(item.quality_run_id) if item.quality_run_id else None,
        "failure": _failure(item.failure),
    }


def _quality(item: QualityRun, *, details: bool = False) -> Mapping[str, object]:
    result: dict[str, object] = {
        "run_id": str(item.run_id),
        "version_id": str(item.version_id),
        "status": item.status.value,
        "rule_version": item.rule_version,
        "schema_version": item.schema_version,
        "started_at": item.started_at.to_iso(),
        "ended_at": item.ended_at.to_iso() if item.ended_at else None,
        "failure": _failure(item.failure),
    }
    if details:
        result["results"] = [
            {
                "rule_code": value.rule_code,
                "status": "PASSED" if value.passed else "FAILED",
                "affected_count": value.affected_count,
                "summary": value.summary,
            }
            for value in item.results
        ]
    return result


def _report(item: DifferenceReport) -> Mapping[str, object]:
    return {
        "report_id": str(item.report_id),
        "version_id": str(item.version_id),
        "primary_source": item.primary_source.value,
        "fallback_source": item.fallback_source.value,
        "created_at": item.created_at.to_iso(),
        "field_differences": dict(item.field_differences),
        "total_compared": item.total_compared,
        "total_differences": item.total_differences,
        "primary_batch_id": str(item.primary_batch_id),
        "fallback_batch_id": str(item.fallback_batch_id),
        "primary_version_id": str(item.primary_version_id),
        "fallback_version_id": str(item.fallback_version_id),
        "primary_quality_run_id": str(item.primary_quality_run_id),
        "fallback_quality_run_id": str(item.fallback_quality_run_id),
        "threshold_rule_version": item.threshold_rule_version,
    }


def _override(item: SourceOverride) -> dict[str, object]:
    return {
        "override_id": str(item.override_id),
        "dataset": item.dataset.value,
        "status": item.status.value,
        "difference_report_id": str(item.difference_report_id),
        "primary_source": item.primary_source.value,
        "fallback_source": item.fallback_source.value,
        "primary_batch_id": str(item.primary_batch_id),
        "fallback_batch_id": str(item.fallback_batch_id),
        "field_replacements": [dict(value) for value in item.field_replacements],
        "fallback_only_keys": [dict(value) for value in item.fallback_only_keys],
        "exact_scope_sha256": item.exact_scope_sha256,
        "reason": item.reason,
        "created_at": item.created_at.to_iso(),
        "approved_at": item.approved_at.to_iso() if item.approved_at else None,
        "approved_by": item.approved_by,
        "applied_at": item.applied_at.to_iso() if item.applied_at else None,
        "applied_version_id": str(item.applied_version_id) if item.applied_version_id else None,
    }


def _job(item: JobRun) -> Mapping[str, object]:
    return {
        "job_id": item.job_id,
        "job_type": item.job_type,
        "status": item.status.value,
        "created_at": item.created_at.to_iso(),
        "started_at": item.started_at.to_iso() if item.started_at else None,
        "ended_at": item.ended_at.to_iso() if item.ended_at else None,
        "stage": item.stage,
        "progress": item.progress,
        "row_version": item.row_version,
        "failure": (
            {
                "code": item.failure.error_type,
                "message": item.failure.message,
                "retryable": item.failure.retryable,
            }
            if item.failure
            else None
        ),
    }


def _json_value(value: object) -> object:
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    return value


class DataCenterQueryService:
    """Compose metadata, verified artifacts and bounded Canonical queries."""

    def __init__(
        self,
        *,
        data_uow_factory: DataUnitOfWorkFactory,
        preview: CanonicalPreviewPort,
        difference_verifier: DifferenceVerifierPort,
        jobs: JobListPort,
        clock: Clock | None = None,
    ) -> None:
        self._uow_factory = data_uow_factory
        self._preview = preview
        self._difference_verifier = difference_verifier
        self._jobs = jobs
        self._clock = clock

    def sources(self) -> list[Mapping[str, object]]:
        return [
            {
                "source": item.source,
                "dataset": item.dataset.value,
                "role": item.role.value,
                "status": item.status.value,
                "evidence_kind": "REAL" if item.status.value == "PRODUCTION_READY" else None,
                "blocking_reason": item.blocking_reason,
                "auto_switch": item.auto_switch,
            }
            for item in default_source_matrix().entries
        ]

    def summary(self) -> Mapping[str, object]:
        datasets: list[Mapping[str, object]] = []
        with self._uow_factory() as uow:
            for dataset in DatasetKind:
                versions = uow.data.list_versions_page(
                    dataset=dataset, status=None, before=None, limit=1
                )
                published = uow.data.list_versions_page(
                    dataset=dataset,
                    status=DatasetVersionStatus.PUBLISHED,
                    before=None,
                    limit=1,
                )
                current = published[0] if published else (versions[0] if versions else None)
                datasets.append(
                    {
                        "dataset": dataset.value,
                        "version": _version(current) if current else None,
                    }
                )
        return {
            "datasets": datasets,
            "failures": len(self._failures(limit=101, before=None)),
            "blocked_sources": sum(item["status"] != "PRODUCTION_READY" for item in self.sources()),
            "generated_at": utc_now(clock=self._clock).to_iso(),
        }

    @staticmethod
    def _page(
        kind: str,
        values: Sequence[Any],
        limit: int,
        mapper: Any,
        timestamp: Any,
        stable_id: Any,
    ) -> DataCenterPage:
        visible = values[:limit]
        next_cursor = None
        if len(values) > limit and visible:
            last = visible[-1]
            next_cursor = _cursor(kind, timestamp(last), stable_id(last))
        return DataCenterPage(tuple(mapper(value) for value in visible), next_cursor)

    def list_batches(
        self,
        *,
        dataset: str | None = None,
        source: str | None = None,
        status: str | None = None,
        started_from: str | None = None,
        started_to: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> DataCenterPage:
        parsed = _parse_cursor(cursor, "batches")
        with self._uow_factory() as uow:
            rows = uow.data.list_batches_page(
                dataset=DatasetKind(dataset) if dataset else None,
                source=DataSource(source) if source else None,
                status=IngestionBatchStatus(status) if status else None,
                started_from=started_from,
                started_to=started_to,
                before=(parsed[0], parsed[1]) if parsed else None,
                limit=limit + 1,
            )
        return self._page(
            "batches",
            rows,
            limit,
            _batch,
            lambda x: x.started_at.to_iso(),
            lambda x: str(x.batch_id),
        )

    def list_versions(
        self,
        *,
        dataset: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> DataCenterPage:
        parsed = _parse_cursor(cursor, "versions")
        with self._uow_factory() as uow:
            rows = uow.data.list_versions_page(
                dataset=DatasetKind(dataset) if dataset else None,
                status=DatasetVersionStatus(status) if status else None,
                before=(parsed[0], parsed[1]) if parsed else None,
                limit=limit + 1,
            )
        return self._page(
            "versions",
            rows,
            limit,
            _version,
            lambda x: x.created_at.to_iso(),
            lambda x: str(x.version_id),
        )

    def get_version(self, version_id: str) -> Mapping[str, object]:
        with self._uow_factory() as uow:
            return _version(uow.data.get_version(version_id))

    def preview(
        self,
        *,
        version_id: str,
        fields: Sequence[str] | None,
        codes: Sequence[str] | None,
        start_date: str | None,
        end_date: str | None,
        cursor: str | None,
        limit: int,
    ) -> DataCenterPage:
        if version_id.casefold() == "latest":
            raise QueryContractError("latest is forbidden")
        with self._uow_factory() as uow:
            version = uow.data.get_version(version_id)
        parsed = _parse_cursor(cursor, f"preview:{version_id}")
        from quantking.data.adapters.canonical_schema import sort_key

        canonical_key = sort_key(version.dataset.value)
        requested_fields = tuple(fields) if fields is not None else None
        query_fields = (
            tuple(dict.fromkeys((*requested_fields, *canonical_key)))
            if requested_fields is not None
            else None
        )
        table = self._preview.query_by_id(
            version_id=version_id,
            dataset=version.dataset.value,
            fields=query_fields,
            codes=codes,
            start_date=TradingDate.from_iso(start_date) if start_date else None,
            end_date=TradingDate.from_iso(end_date) if end_date else None,
            after_sort_key=parsed[2] if parsed else None,
            limit=limit + 1,
        )
        rows = list(table.to_pylist())
        visible = rows[:limit]
        next_cursor = None
        if len(rows) > limit and visible:
            last = visible[-1]
            values = tuple(str(last[name]) for name in canonical_key)
            next_cursor = _cursor(
                f"preview:{version_id}", version.created_at.to_iso(), version_id, values
            )
        if requested_fields is not None:
            visible = [{name: row[name] for name in requested_fields} for row in visible]
        return DataCenterPage(
            tuple({name: _json_value(value) for name, value in row.items()} for row in visible),
            next_cursor,
        )

    def list_quality_runs(
        self,
        *,
        version_id: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> DataCenterPage:
        parsed = _parse_cursor(cursor, "quality")
        with self._uow_factory() as uow:
            rows = uow.data.list_quality_runs_page(
                version_id=version_id,
                status=QualityRunStatus(status) if status else None,
                before=(parsed[0], parsed[1]) if parsed else None,
                limit=limit + 1,
            )
        return self._page(
            "quality",
            rows,
            limit,
            _quality,
            lambda x: x.started_at.to_iso(),
            lambda x: str(x.run_id),
        )

    def get_quality_run(self, run_id: str) -> Mapping[str, object]:
        with self._uow_factory() as uow:
            return _quality(uow.data.get_quality_run(run_id), details=True)

    def list_difference_reports(
        self,
        *,
        version_id: str | None = None,
        primary_source: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> DataCenterPage:
        parsed = _parse_cursor(cursor, "differences")
        with self._uow_factory() as uow:
            rows = uow.data.list_difference_reports_page(
                version_id=version_id,
                primary_source=DataSource(primary_source) if primary_source else None,
                before=(parsed[0], parsed[1]) if parsed else None,
                limit=limit + 1,
            )
        return self._page(
            "differences",
            rows,
            limit,
            _report,
            lambda x: x.created_at.to_iso(),
            lambda x: str(x.report_id),
        )

    def get_difference_report(self, report_id: str) -> Mapping[str, object]:
        with self._uow_factory() as uow:
            report = uow.data.get_difference_report(report_id)
        verified = self._difference_verifier.verify(report_id=parse_entity_id(report_id))
        if verified.report_sha256 != report.report_sha256:
            raise InvalidDataCenterQueryError("difference report credentials are invalid")
        return _report(report)

    def difference_rows(self, *, report_id: str, cursor: str | None, limit: int) -> DataCenterPage:
        parsed = _parse_cursor(cursor, f"difference-rows:{report_id}")
        verified = self._difference_verifier.verify(report_id=parse_entity_id(report_id))
        rows = list(verified.row_differences)
        start = int(parsed[1]) if parsed else 0
        visible = rows[start : start + limit]
        next_cursor = None
        if start + limit < len(rows):
            next_cursor = _cursor(
                f"difference-rows:{report_id}",
                verified.created_at.to_iso(),
                str(start + limit),
            )
        return DataCenterPage(tuple(visible), next_cursor)

    def list_source_overrides(
        self,
        *,
        dataset: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> DataCenterPage:
        parsed = _parse_cursor(cursor, "overrides")
        with self._uow_factory() as uow:
            rows = uow.data.list_overrides_page(
                dataset=DatasetKind(dataset) if dataset else None,
                status=SourceOverrideStatus(status) if status else None,
                before=(parsed[0], parsed[1]) if parsed else None,
                limit=limit + 1,
            )
        return self._page(
            "overrides",
            rows,
            limit,
            _override,
            lambda x: x.created_at.to_iso(),
            lambda x: str(x.override_id),
        )

    def get_source_override(self, override_id: str) -> Mapping[str, object]:
        with self._uow_factory() as uow:
            override = uow.data.get_override(override_id)
            uow.data.get_difference_report(str(override.difference_report_id))
            result = _override(override)
            self._difference_verifier.verify(report_id=override.difference_report_id)
            binding: OverrideBinding | None
            attempt: OverrideAttempt | None
            try:
                binding = uow.data.get_override_application_binding(override_id)
                attempt = uow.data.find_last_override_application_attempt(override_id)
            except Exception as error:
                if override.status is SourceOverrideStatus.APPLIED:
                    raise InvalidDataCenterQueryError(
                        "override application lineage is invalid"
                    ) from error
                binding = None
                attempt = None
        result["application"] = (
            {
                "status": binding.status,
                "child_version_id": str(binding.child_version_id),
                "report_sha256": binding.report_sha256,
            }
            if binding
            else None
        )
        result["attempt"] = (
            {
                "status": attempt.status,
                "child_version_id": str(attempt.child_version_id),
                "failure_message": attempt.failure_message,
            }
            if attempt
            else None
        )
        return result

    def list_eod_runs(
        self,
        *,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> DataCenterPage:
        parsed = _parse_cursor(cursor, "eod")
        rows = self._jobs.list_jobs_page(
            job_type="data.eod.v1",
            status=JobStatus(status) if status else None,
            before=(parsed[0], parsed[1]) if parsed else None,
            limit=limit + 1,
        )
        return self._page(
            "eod", rows, limit, _job, lambda x: x.created_at.to_iso(), lambda x: x.job_id
        )

    def _failures(
        self, *, limit: int, before: tuple[str, str] | None
    ) -> list[Mapping[str, object]]:
        with self._uow_factory() as uow:
            batches = uow.data.list_batches_page(
                dataset=None,
                source=None,
                status=IngestionBatchStatus.FAILED,
                started_from=None,
                started_to=None,
                before=before,
                limit=limit,
            )
            versions = uow.data.list_versions_page(
                dataset=None,
                status=DatasetVersionStatus.FAILED,
                before=before,
                limit=limit,
            )
            qualities = uow.data.list_quality_runs_page(
                version_id=None,
                status=QualityRunStatus.FAILED,
                before=before,
                limit=limit,
            )
        jobs = self._jobs.list_jobs_page(
            job_type="data.eod.v1",
            status=JobStatus.FAILED,
            before=before,
            limit=limit,
        )
        failures: list[Mapping[str, object]] = []
        for kind, entities in (
            ("BATCH", batches),
            ("VERSION", versions),
            ("QUALITY", qualities),
            ("EOD", jobs),
        ):
            for entity in entities:
                stable_id = str(
                    getattr(
                        entity,
                        {"BATCH": "batch_id", "VERSION": "version_id", "QUALITY": "run_id"}.get(
                            kind, "job_id"
                        ),
                    )
                )
                timestamp = (
                    entity.started_at
                    if isinstance(entity, (IngestionBatch, QualityRun))
                    else entity.created_at
                )
                failure = getattr(entity, "failure", None)
                failures.append(
                    {
                        "kind": kind,
                        "entity_id": stable_id,
                        "stage": getattr(entity, "stage", None) or kind.casefold(),
                        "occurred_at": timestamp.to_iso(),
                        "code": (
                            getattr(failure.error_type, "value", failure.error_type)
                            if failure
                            else "QUALITY_RULE_FAILED"
                        ),
                        "message": failure.message if failure else "质量规则未通过。",
                        "_sort_id": stable_id,
                    }
                )
        return sorted(
            failures,
            key=lambda value: (str(value["occurred_at"]), str(value["_sort_id"])),
            reverse=True,
        )[:limit]

    def list_failures(
        self, *, cursor: str | None = None, limit: int = DEFAULT_PAGE_LIMIT
    ) -> DataCenterPage:
        parsed = _parse_cursor(cursor, "failures")
        rows = self._failures(limit=limit + 1, before=(parsed[0], parsed[1]) if parsed else None)
        visible = rows[:limit]
        next_cursor = None
        if len(rows) > limit and visible:
            last = visible[-1]
            next_cursor = _cursor("failures", str(last["occurred_at"]), str(last["_sort_id"]))
        return DataCenterPage(
            tuple(
                {key: value for key, value in row.items() if key != "_sort_id"} for row in visible
            ),
            next_cursor,
        )


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "MAX_PREVIEW_LIMIT",
    "DataCenterPage",
    "DataCenterQueryService",
    "InvalidDataCenterQueryError",
    "validate_cursor",
]
