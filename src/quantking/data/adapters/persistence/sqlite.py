"""SQLAlchemy repository and unit-of-work for Stage 1 data metadata.

This adapter maps the framework-free domain objects in
:mod:`quantking.data.models` to and from the ORM rows in
:mod:`quantking.data.adapters.persistence.models`. It reuses the engine and
pragmas from the jobs persistence adapter (``create_sqlite_engine``) but does
not depend on jobs domain logic. Immutability of PUBLISHED versions is enforced
both at the service layer and by the Alembic database triggers.

Short transactions, one fresh session per call; ``SQLITE_BUSY``/``SQLITE_LOCKED``
retries are delegated to the same retry boundary used by jobs. No Parquet row
data is ever written here — only summaries, counts, managed report identifiers
and manifest SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Self, TypeVar

from sqlalchemy import Engine, and_, delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from quantking.common.errors import (
    DatasetVersionImmutableError,
    DatasetVersionNotFoundError,
    InvalidDataTransitionError,
    InvalidIdError,
)
from quantking.common.ids import parse_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.persistence.models import (
    DatasetVersionBatchRow,
    DatasetVersionRow,
    DifferenceReportRow,
    IngestionBatchRow,
    QualityResultRow,
    QualityRunRow,
    ResolutionRunRow,
)
from quantking.data.comparison.verifier import VerifiedDifferenceReport
from quantking.data.models import (
    BATCH_TRANSITIONS,
    DATASET_VERSION_TRANSITIONS,
    BatchFailureInfo,
    BatchFailureKind,
    DataProvider,
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    DataUpstream,
    DifferenceReport,
    EmptyResultInfo,
    IngestionBatch,
    IngestionBatchStatus,
    LegalEmptyClassification,
    ManifestFileEntry,
    QualityResult,
    QualityRun,
    QualityRunStatus,
)
from quantking.data.resolution.artifacts import VerifiedResolutionArtifact
from quantking.data.resolution.models import ResolutionRun, ResolutionRunRecord
from quantking.jobs.adapters.persistence.models import AuditRecordRow
from quantking.jobs.adapters.persistence.sqlite import (
    create_sqlite_engine as _create_jobs_engine,
)
from quantking.jobs.adapters.persistence.sqlite import (
    execute_with_retry,
)
from quantking.jobs.models import AuditRecord, RetryPolicy

ResultT = TypeVar("ResultT")

_IMMUTABLE_VERSION_STATUSES = frozenset({DatasetVersionStatus.PUBLISHED})
_TERMINAL_BATCH_STATUSES = frozenset(
    {
        IngestionBatchStatus.SUCCEEDED,
        IngestionBatchStatus.PARTIAL,
        IngestionBatchStatus.FAILED,
    }
)


def create_data_engine(database_path: Path, *, busy_timeout_ms: int) -> Engine:
    """Create a SQLAlchemy engine with the same connection pragmas as jobs.

    Delegates to the shared ``create_sqlite_engine`` helper so every connection
    sets ``foreign_keys=ON``, ``journal_mode=WAL`` and ``busy_timeout``.
    """
    if not isinstance(database_path, Path) or not database_path.is_absolute():
        raise ValueError("SQLite database path must be absolute")
    return _create_jobs_engine(database_path, busy_timeout_ms=busy_timeout_ms)


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _json_mapping(value: Mapping[str, str]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_sequence(value: tuple[object, ...]) -> str:
    return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))


def _load_mapping(value: str) -> dict[str, str]:
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("persisted mapping is invalid")
    return {str(k): str(v) for k, v in loaded.items()}


def _load_str_sequence(value: str) -> tuple[str, ...]:
    loaded = json.loads(value)
    if not isinstance(loaded, list):
        raise ValueError("persisted sequence is invalid")
    return tuple(str(item) for item in loaded)


def _failure_json(value: BatchFailureInfo | None) -> str | None:
    if value is None:
        return None
    return json.dumps(
        {
            "error_type": value.error_type.value,
            "message": value.message,
            "retryable": value.retryable,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _load_failure(value: str | None) -> BatchFailureInfo | None:
    if value is None:
        return None
    loaded = json.loads(value)
    return BatchFailureInfo(
        error_type=BatchFailureKind(str(loaded["error_type"])),
        message=str(loaded["message"]),
        retryable=bool(loaded["retryable"]),
    )


def _timestamp(value: str | None) -> UtcTimestamp | None:
    return None if value is None else UtcTimestamp.from_iso(value)


def _sha256_of_json(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _empty_result_json(value: EmptyResultInfo | None) -> str | None:
    if value is None:
        return None
    return json.dumps(
        {"classification": value.classification.value, "reason": value.reason},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _load_empty_result(value: str | None) -> EmptyResultInfo | None:
    if value is None:
        return None
    loaded = json.loads(value)
    return EmptyResultInfo(
        classification=LegalEmptyClassification(str(loaded["classification"])),
        reason=str(loaded["reason"]),
    )


def _row_matches(row: object, values: Mapping[str, object]) -> bool:
    return all(getattr(row, key) == value for key, value in values.items())


# ---------------------------------------------------------------------------
# Batch mapping
# ---------------------------------------------------------------------------


def _batch_row_values(batch: IngestionBatch) -> dict[str, object]:
    manifest_json = _json_sequence(
        tuple(
            {"relative_path": f.relative_path, "size": f.size, "sha256": f.sha256}
            for f in batch.manifest_files
        )
    )
    return {
        "batch_id": str(batch.batch_id),
        "source": batch.upstream.value,
        "dataset": batch.dataset.value,
        "status": batch.status.value,
        "adapter_version": batch.adapter_version,
        "request_params_json": batch.request_params_json,
        "request_params_sha256": _sha256_of_json(batch.request_params_json),
        "started_at": batch.started_at.to_iso(),
        "ended_at": None if batch.ended_at is None else batch.ended_at.to_iso(),
        "row_count": batch.row_count,
        "security_count": batch.security_count,
        "manifest_files_json": manifest_json,
        "failure_json": _failure_json(batch.failure),
        "empty_result_json": _empty_result_json(batch.empty_result),
        "job_run_id": batch.job_run_id,
    }


def _batch_domain(row: IngestionBatchRow) -> IngestionBatch:
    from quantking.common.ids import parse_entity_id

    raw_manifest = json.loads(row.manifest_files_json)
    manifest_files = tuple(
        ManifestFileEntry(
            relative_path=str(item["relative_path"]),
            size=int(item["size"]),
            sha256=str(item["sha256"]),
        )
        for item in raw_manifest
    )
    request_params = _load_mapping(row.request_params_json)
    provider = DataProvider(request_params["provider"])
    version_key = "baostock_version" if provider is DataProvider.BAOSTOCK else "akshare_version"
    return IngestionBatch(
        batch_id=parse_entity_id(row.batch_id),
        provider=provider,
        upstream=DataUpstream(row.source),
        endpoint=request_params["endpoint"],
        library_version=request_params[version_key],
        dataset=DatasetKind(row.dataset),
        status=IngestionBatchStatus(row.status),
        adapter_version=row.adapter_version,
        request_params=request_params,
        request_params_json=row.request_params_json,
        started_at=UtcTimestamp.from_iso(row.started_at),
        ended_at=_timestamp(row.ended_at),
        row_count=row.row_count,
        security_count=row.security_count,
        manifest_files=manifest_files,
        failure=_load_failure(row.failure_json),
        job_run_id=row.job_run_id,
        empty_result=_load_empty_result(row.empty_result_json),
    )


# ---------------------------------------------------------------------------
# Version mapping
# ---------------------------------------------------------------------------


def _version_row_values(version: DatasetVersion) -> dict[str, object]:
    parent = None if version.parent_version_id is None else str(version.parent_version_id)
    input_ids = tuple(str(bid) for bid in version.input_batch_ids)
    return {
        "version_id": str(version.version_id),
        "dataset": version.dataset.value,
        "status": version.status.value,
        "schema_version": version.schema_version,
        "code_version": version.code_version,
        "rule_version": version.rule_version,
        "parent_version_id": parent,
        "input_batch_ids_json": _json_sequence(input_ids),
        "created_at": version.created_at.to_iso(),
        "validated_at": None if version.validated_at is None else version.validated_at.to_iso(),
        "published_at": None if version.published_at is None else version.published_at.to_iso(),
        "manifest_sha256": version.manifest_sha256,
        "security_count": version.security_count,
        "row_count": version.row_count,
        "coverage_start": None if version.coverage_start is None else str(version.coverage_start),
        "coverage_end": None if version.coverage_end is None else str(version.coverage_end),
        "coverage_summary_json": _json_mapping(version.coverage_summary),
        "failure_json": _failure_json(version.failure),
        "quality_run_id": None if version.quality_run_id is None else str(version.quality_run_id),
        "content_sha256": version.content_sha256,
    }


def _version_domain(session: Session, row: DatasetVersionRow) -> DatasetVersion:
    from quantking.common.ids import parse_entity_id

    lineage_rows = session.scalars(
        select(DatasetVersionBatchRow)
        .where(DatasetVersionBatchRow.version_id == row.version_id)
        .order_by(DatasetVersionBatchRow.ordinal)
    )
    input_ids = tuple(parse_entity_id(item.batch_id) for item in lineage_rows)
    parent = None if row.parent_version_id is None else parse_entity_id(row.parent_version_id)
    return DatasetVersion(
        version_id=parse_entity_id(row.version_id),
        dataset=DatasetKind(row.dataset),
        status=DatasetVersionStatus(row.status),
        schema_version=row.schema_version,
        code_version=row.code_version,
        rule_version=row.rule_version,
        parent_version_id=parent,
        input_batch_ids=input_ids,
        created_at=UtcTimestamp.from_iso(row.created_at),
        validated_at=_timestamp(row.validated_at),
        published_at=_timestamp(row.published_at),
        manifest_sha256=row.manifest_sha256,
        security_count=row.security_count,
        row_count=row.row_count,
        coverage_start=None
        if row.coverage_start is None
        else TradingDate.from_iso(row.coverage_start),
        coverage_end=None if row.coverage_end is None else TradingDate.from_iso(row.coverage_end),
        coverage_summary=_load_mapping(row.coverage_summary_json),
        failure=_load_failure(row.failure_json),
        quality_run_id=None if row.quality_run_id is None else parse_entity_id(row.quality_run_id),
        content_sha256=row.content_sha256,
    )


# ---------------------------------------------------------------------------
# Quality and difference-report mapping
# ---------------------------------------------------------------------------


def _quality_run_row_values(run: QualityRun) -> dict[str, object]:
    results_json = json.dumps(
        [
            {
                "rule_code": result.rule_code,
                "passed": result.passed,
                "affected_count": result.affected_count,
                "summary": result.summary,
                "report_relative_path": result.report_relative_path,
            }
            for result in run.results
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "run_id": str(run.run_id),
        "version_id": str(run.version_id),
        "status": run.status.value,
        "rule_version": run.rule_version,
        "schema_version": run.schema_version,
        "content_sha256": run.content_sha256,
        "started_at": run.started_at.to_iso(),
        "ended_at": None if run.ended_at is None else run.ended_at.to_iso(),
        "results_json": results_json,
        "summary_report_relative_path": run.summary_report_relative_path,
        "failure_json": _failure_json(run.failure),
    }


def _quality_run_domain(session: Session, row: QualityRunRow) -> QualityRun:
    from quantking.common.ids import parse_entity_id

    result_rows = session.scalars(
        select(QualityResultRow)
        .where(QualityResultRow.run_id == row.run_id)
        .order_by(QualityResultRow.sequence_number)
    )
    results = tuple(
        QualityResult(
            rule_code=result.rule_code,
            passed=bool(result.passed),
            affected_count=result.affected_count,
            summary=result.summary,
            report_relative_path=result.report_relative_path,
        )
        for result in result_rows
    )
    return QualityRun(
        run_id=parse_entity_id(row.run_id),
        version_id=parse_entity_id(row.version_id),
        status=QualityRunStatus(row.status),
        rule_version=row.rule_version,
        schema_version=row.schema_version,
        content_sha256=row.content_sha256,
        started_at=UtcTimestamp.from_iso(row.started_at),
        ended_at=_timestamp(row.ended_at),
        results=results,
        summary_report_relative_path=row.summary_report_relative_path,
        failure=_load_failure(row.failure_json),
    )


def _difference_row_values(report: DifferenceReport) -> dict[str, object]:
    return {
        "report_id": str(report.report_id),
        "version_id": str(report.version_id),
        "primary_source": report.primary_source.value,
        "fallback_source": report.fallback_source.value,
        "created_at": report.created_at.to_iso(),
        "field_differences_json": _json_mapping(report.field_differences),
        "total_compared": report.total_compared,
        "total_differences": report.total_differences,
        "report_relative_path": report.report_relative_path,
        "primary_batch_id": str(report.primary_batch_id),
        "fallback_batch_id": str(report.fallback_batch_id),
        "primary_version_id": str(report.primary_version_id),
        "fallback_version_id": str(report.fallback_version_id),
        "primary_content_sha256": report.primary_content_sha256,
        "fallback_content_sha256": report.fallback_content_sha256,
        "primary_quality_run_id": str(report.primary_quality_run_id),
        "fallback_quality_run_id": str(report.fallback_quality_run_id),
        "sample_spec_sha256": report.sample_spec_sha256,
        "threshold_rule_version": report.threshold_rule_version,
        "report_sha256": report.report_sha256,
    }


def _difference_from_verified(report: VerifiedDifferenceReport) -> DifferenceReport:
    shared_candidate = report.primary_version_id == report.fallback_version_id
    if shared_candidate and (
        report.manifest_schema_version
        not in {
            "difference-report-manifest.k2.v2",
            "difference-report-manifest.k4.v4",
        }
        or report.binding_mode != "SHARED_RESOLUTION_CANDIDATE"
    ):
        raise InvalidDataTransitionError(
            "shared DifferenceReport requires a verified shared-candidate artifact schema"
        )
    return DifferenceReport(
        report_id=report.report_id,
        version_id=report.primary_version_id,
        primary_source=DataUpstream(report.primary_source),
        fallback_source=DataUpstream(report.fallback_source),
        created_at=report.created_at,
        field_differences=report.field_differences,
        total_compared=report.total_compared,
        total_differences=report.total_differences,
        report_relative_path=report.report_relative_path,
        primary_batch_id=report.primary_batch_id,
        fallback_batch_id=report.fallback_batch_id,
        primary_version_id=report.primary_version_id,
        fallback_version_id=report.fallback_version_id,
        primary_content_sha256=report.primary_content_sha256,
        fallback_content_sha256=report.fallback_content_sha256,
        primary_quality_run_id=report.primary_quality_run_id,
        fallback_quality_run_id=report.fallback_quality_run_id,
        sample_spec_sha256=report.sample_spec_sha256,
        threshold_rule_version=report.threshold_rule_version,
        report_sha256=report.report_sha256,
    )


def _difference_domain(row: DifferenceReportRow) -> DifferenceReport:
    from quantking.common.ids import parse_entity_id

    return DifferenceReport(
        report_id=parse_entity_id(row.report_id),
        version_id=parse_entity_id(row.version_id),
        primary_source=DataUpstream(row.primary_source),
        fallback_source=DataUpstream(row.fallback_source),
        created_at=UtcTimestamp.from_iso(row.created_at),
        field_differences=_load_mapping(row.field_differences_json),
        total_compared=row.total_compared,
        total_differences=row.total_differences,
        report_relative_path=row.report_relative_path,
        primary_batch_id=parse_entity_id(row.primary_batch_id),
        fallback_batch_id=parse_entity_id(row.fallback_batch_id),
        primary_version_id=parse_entity_id(row.primary_version_id),
        fallback_version_id=parse_entity_id(row.fallback_version_id),
        primary_content_sha256=row.primary_content_sha256,
        fallback_content_sha256=row.fallback_content_sha256,
        primary_quality_run_id=parse_entity_id(row.primary_quality_run_id),
        fallback_quality_run_id=parse_entity_id(row.fallback_quality_run_id),
        sample_spec_sha256=row.sample_spec_sha256,
        threshold_rule_version=row.threshold_rule_version,
        report_sha256=row.report_sha256,
    )


def _resolution_row_values(
    run: ResolutionRun,
    artifact: VerifiedResolutionArtifact,
) -> dict[str, object]:
    if artifact.run_id != run.run_id or artifact.summary != run.summary_dict():
        raise InvalidDataTransitionError("resolution artifact does not match run")
    return {
        "run_id": str(run.run_id),
        "dataset": run.dataset.value,
        "created_job_id": str(run.created_job_id),
        "candidate_version_id": (
            None if run.candidate_version_id is None else str(run.candidate_version_id)
        ),
        "created_at": run.created_at.to_iso(),
        "rule_version": run.rule_version,
        "threshold_rule_version": run.threshold_rule_version,
        "input_fingerprint": run.input_fingerprint,
        "artifact_relative_path": artifact.relative_path,
        "manifest_sha256": artifact.manifest_sha256,
        "total_decisions": run.total_decisions,
        "default_decision_count": run.default_decision_count,
        "exception_count": len(run.exceptions),
        "conflict_count": len(run.conflicts),
        "blocker_count": len(run.run_blockers),
    }


def _resolution_domain(row: ResolutionRunRow) -> ResolutionRunRecord:
    return ResolutionRunRecord(
        run_id=parse_entity_id(row.run_id),
        dataset=DatasetKind(row.dataset),
        created_job_id=parse_entity_id(row.created_job_id),
        candidate_version_id=(
            None if row.candidate_version_id is None else parse_entity_id(row.candidate_version_id)
        ),
        created_at=UtcTimestamp.from_iso(row.created_at),
        rule_version=row.rule_version,
        threshold_rule_version=row.threshold_rule_version,
        input_fingerprint=row.input_fingerprint,
        artifact_relative_path=row.artifact_relative_path,
        manifest_sha256=row.manifest_sha256,
        total_decisions=row.total_decisions,
        default_decision_count=row.default_decision_count,
        exception_count=row.exception_count,
        conflict_count=row.conflict_count,
        blocker_count=row.blocker_count,
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class DataRepository:
    """Repository scoped to one SQLAlchemy session and one short transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- batches -----------------------------------------------------------

    def save_batch(self, batch: IngestionBatch) -> IngestionBatch:
        """Insert or update an ingestion batch row."""
        existing = self._session.get(IngestionBatchRow, str(batch.batch_id))
        values = _batch_row_values(batch)
        if existing is None:
            self._session.add(IngestionBatchRow(**values))
        else:
            if _row_matches(existing, values):
                return batch
            current = IngestionBatchStatus(existing.status)
            if current in _TERMINAL_BATCH_STATUSES:
                raise InvalidDataTransitionError(
                    "terminal ingestion batch content is immutable; retry must create a new id",
                    context={"batch_id": str(batch.batch_id), "current": existing.status},
                )
            if batch.status not in BATCH_TRANSITIONS[current]:
                raise InvalidDataTransitionError(
                    "ingestion batch transition is not allowed",
                    context={"current": current.value, "target": batch.status.value},
                )
            for key, value in values.items():
                setattr(existing, key, value)
        self._session.flush()
        return batch

    def get_batch(self, batch_id: str) -> IngestionBatch:
        row = self._session.get(IngestionBatchRow, batch_id)
        if row is None:
            raise InvalidIdError("ingestion batch does not exist", context={"batch_id": batch_id})
        return _batch_domain(row)

    def count_failed_entities(self) -> int:
        """Count every entity represented by the unified failure view in SQL."""
        counts = (
            self._session.scalar(
                select(func.count())
                .select_from(IngestionBatchRow)
                .where(IngestionBatchRow.status == IngestionBatchStatus.FAILED.value)
            ),
            self._session.scalar(
                select(func.count())
                .select_from(DatasetVersionRow)
                .where(DatasetVersionRow.status == DatasetVersionStatus.FAILED.value)
            ),
            self._session.scalar(
                select(func.count())
                .select_from(QualityRunRow)
                .where(QualityRunRow.status == QualityRunStatus.FAILED.value)
            ),
        )
        return sum(int(value or 0) for value in counts)

    def list_batches_page(
        self,
        *,
        dataset: DatasetKind | None,
        upstream: DataUpstream | None,
        status: IngestionBatchStatus | None,
        started_from: str | None,
        started_to: str | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[IngestionBatch]:
        """Return one newest-first keyset page without materialising the table."""
        statement = select(IngestionBatchRow)
        if dataset is not None:
            statement = statement.where(IngestionBatchRow.dataset == dataset.value)
        if upstream is not None:
            statement = statement.where(IngestionBatchRow.source == upstream.value)
        if status is not None:
            statement = statement.where(IngestionBatchRow.status == status.value)
        if started_from is not None:
            statement = statement.where(IngestionBatchRow.started_at >= started_from)
        if started_to is not None:
            statement = statement.where(IngestionBatchRow.started_at <= started_to)
        if before is not None:
            timestamp, stable_id = before
            statement = statement.where(
                or_(
                    IngestionBatchRow.started_at < timestamp,
                    and_(
                        IngestionBatchRow.started_at == timestamp,
                        IngestionBatchRow.batch_id < stable_id,
                    ),
                )
            )
        statement = statement.order_by(
            IngestionBatchRow.started_at.desc(), IngestionBatchRow.batch_id.desc()
        ).limit(limit)
        return [_batch_domain(row) for row in self._session.scalars(statement)]

    def list_batches_for_job(self, job_run_id: str) -> list[IngestionBatch]:
        statement = (
            select(IngestionBatchRow)
            .where(IngestionBatchRow.job_run_id == job_run_id)
            .order_by(IngestionBatchRow.batch_id)
        )
        return [_batch_domain(row) for row in self._session.scalars(statement)]

    # -- versions ----------------------------------------------------------

    def save_version(self, version: DatasetVersion) -> DatasetVersion:
        """Insert or update a dataset version row with immutability guards."""
        if version.status in {DatasetVersionStatus.VALIDATED, DatasetVersionStatus.PUBLISHED} and (
            version.quality_run_id is None or version.content_sha256 is None
        ):
            exact_runs = [
                run
                for run in self.list_quality_runs(
                    version_id=str(version.version_id), status=QualityRunStatus.PASSED
                )
                if run.rule_version == version.rule_version
                and run.schema_version == version.schema_version
            ]
            if len(exact_runs) == 1:
                version = replace(
                    version,
                    quality_run_id=exact_runs[0].run_id,
                    content_sha256=exact_runs[0].content_sha256,
                )
        existing = self._session.get(DatasetVersionRow, str(version.version_id))
        values = _version_row_values(version)
        if existing is not None:
            if _row_matches(existing, values):
                return version
            if existing.status in {s.value for s in _IMMUTABLE_VERSION_STATUSES}:
                raise DatasetVersionImmutableError(
                    "published dataset version is immutable",
                    context={"version_id": str(version.version_id)},
                )
            if existing.status == DatasetVersionStatus.FAILED.value:
                raise InvalidDataTransitionError(
                    "failed dataset version cannot be mutated; retry must create a new version",
                    context={"version_id": str(version.version_id)},
                )
            current = DatasetVersionStatus(existing.status)
            if version.status not in DATASET_VERSION_TRANSITIONS[current]:
                raise InvalidDataTransitionError(
                    "dataset version transition is not allowed",
                    context={"current": current.value, "target": version.status.value},
                )
            existing_domain = _version_domain(self._session, existing)
            if (
                existing_domain.parent_version_id != version.parent_version_id
                or existing_domain.input_batch_ids != version.input_batch_ids
                or existing_domain.dataset is not version.dataset
            ):
                raise InvalidDataTransitionError("dataset version lineage is immutable")
            for key, value in values.items():
                setattr(existing, key, value)
        else:
            self._validate_version_lineage(version)
            self._session.add(DatasetVersionRow(**values))
            self._session.flush()
            for ordinal, batch_id in enumerate(version.input_batch_ids):
                self._session.add(
                    DatasetVersionBatchRow(
                        version_id=str(version.version_id),
                        batch_id=str(batch_id),
                        ordinal=ordinal,
                    )
                )
        self._session.flush()
        return version

    def _validate_version_lineage(self, version: DatasetVersion) -> None:
        if version.parent_version_id is not None:
            parent = self._session.get(DatasetVersionRow, str(version.parent_version_id))
            if parent is None or parent.dataset != version.dataset.value:
                raise InvalidDataTransitionError(
                    "parent dataset version must exist in the same dataset"
                )
        for batch_id in version.input_batch_ids:
            batch = self._session.get(IngestionBatchRow, str(batch_id))
            if batch is None or batch.dataset != version.dataset.value:
                raise InvalidDataTransitionError(
                    "input batch must exist and belong to the version dataset"
                )

    def get_version(self, version_id: str) -> DatasetVersion:
        row = self._session.get(DatasetVersionRow, version_id)
        if row is None:
            raise DatasetVersionNotFoundError(
                "dataset version does not exist", context={"version_id": version_id}
            )
        return _version_domain(self._session, row)

    def list_versions(
        self,
        *,
        dataset: DatasetKind | None = None,
        status: DatasetVersionStatus | None = None,
    ) -> list[DatasetVersion]:
        statement = select(DatasetVersionRow).order_by(
            DatasetVersionRow.created_at,
            DatasetVersionRow.version_id,
        )
        if dataset is not None:
            statement = statement.where(DatasetVersionRow.dataset == dataset.value)
        if status is not None:
            statement = statement.where(DatasetVersionRow.status == status.value)
        return [_version_domain(self._session, row) for row in self._session.scalars(statement)]

    def list_versions_page(
        self,
        *,
        dataset: DatasetKind | None,
        status: DatasetVersionStatus | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[DatasetVersion]:
        statement = select(DatasetVersionRow)
        if dataset is not None:
            statement = statement.where(DatasetVersionRow.dataset == dataset.value)
        if status is not None:
            statement = statement.where(DatasetVersionRow.status == status.value)
        if before is not None:
            timestamp, stable_id = before
            statement = statement.where(
                or_(
                    DatasetVersionRow.created_at < timestamp,
                    and_(
                        DatasetVersionRow.created_at == timestamp,
                        DatasetVersionRow.version_id < stable_id,
                    ),
                )
            )
        statement = statement.order_by(
            DatasetVersionRow.created_at.desc(), DatasetVersionRow.version_id.desc()
        ).limit(limit)
        return [_version_domain(self._session, row) for row in self._session.scalars(statement)]

    # -- quality runs ------------------------------------------------------

    def save_quality_run(self, run: QualityRun) -> QualityRun:
        """Persist a quality run and its ordered rule results."""
        if self._session.get(DatasetVersionRow, str(run.version_id)) is None:
            raise DatasetVersionNotFoundError("quality run dataset version does not exist")
        row = self._session.get(QualityRunRow, str(run.run_id))
        values = _quality_run_row_values(run)
        if row is None:
            row = QualityRunRow(**values)
            self._session.add(row)
        else:
            existing_domain = _quality_run_domain(self._session, row)
            if existing_domain == run:
                return run
            if existing_domain.status is not QualityRunStatus.RUNNING:
                raise InvalidDataTransitionError("terminal quality run is immutable")
            if run.status is QualityRunStatus.RUNNING:
                raise InvalidDataTransitionError("RUNNING quality run content is immutable")
            for key, value in values.items():
                setattr(row, key, value)
            self._session.execute(
                delete(QualityResultRow).where(QualityResultRow.run_id == str(run.run_id))
            )
        self._session.flush()
        for sequence_number, result in enumerate(run.results):
            self._session.add(
                QualityResultRow(
                    run_id=str(run.run_id),
                    sequence_number=sequence_number,
                    rule_code=result.rule_code,
                    passed=int(result.passed),
                    affected_count=result.affected_count,
                    summary=result.summary,
                    report_relative_path=result.report_relative_path,
                )
            )
        self._session.flush()
        return run

    def get_quality_run(self, run_id: str) -> QualityRun:
        row = self._session.get(QualityRunRow, run_id)
        if row is None:
            raise InvalidIdError("quality run does not exist", context={"run_id": run_id})
        return _quality_run_domain(self._session, row)

    def list_quality_runs(
        self,
        *,
        version_id: str | None = None,
        status: QualityRunStatus | None = None,
    ) -> list[QualityRun]:
        statement = select(QualityRunRow).order_by(QualityRunRow.started_at, QualityRunRow.run_id)
        if version_id is not None:
            statement = statement.where(QualityRunRow.version_id == version_id)
        if status is not None:
            statement = statement.where(QualityRunRow.status == status.value)
        return [_quality_run_domain(self._session, row) for row in self._session.scalars(statement)]

    def list_quality_runs_page(
        self,
        *,
        version_id: str | None,
        status: QualityRunStatus | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[QualityRun]:
        statement = select(QualityRunRow)
        if version_id is not None:
            statement = statement.where(QualityRunRow.version_id == version_id)
        if status is not None:
            statement = statement.where(QualityRunRow.status == status.value)
        if before is not None:
            timestamp, stable_id = before
            statement = statement.where(
                or_(
                    QualityRunRow.started_at < timestamp,
                    and_(
                        QualityRunRow.started_at == timestamp,
                        QualityRunRow.run_id < stable_id,
                    ),
                )
            )
        statement = statement.order_by(
            QualityRunRow.started_at.desc(), QualityRunRow.run_id.desc()
        ).limit(limit)
        return [_quality_run_domain(self._session, row) for row in self._session.scalars(statement)]

    # -- difference reports ----------------------------------------------

    def save_difference_report(self, report: VerifiedDifferenceReport) -> DifferenceReport:
        if not isinstance(report, VerifiedDifferenceReport):
            raise TypeError("save_difference_report requires a VerifiedDifferenceReport")
        domain = _difference_from_verified(report)
        if self._session.get(DatasetVersionRow, str(domain.version_id)) is None:
            raise DatasetVersionNotFoundError("difference report dataset version does not exist")
        self._validate_difference_report_bindings(domain, verified=report)
        row = self._session.get(DifferenceReportRow, str(domain.report_id))
        values = _difference_row_values(domain)
        if row is None:
            self._session.add(DifferenceReportRow(**values))
        elif not _row_matches(row, values):
            raise InvalidDataTransitionError("difference reports are immutable")
        self._session.flush()
        return domain

    def _validate_difference_report_bindings(
        self,
        report: DifferenceReport,
        *,
        verified: VerifiedDifferenceReport,
    ) -> None:
        """Verify both-side batches, sources, datasets and quality runs exist and match.

        A DifferenceReport must not be persisted if either side references a
        batch, version or quality run that does not exist or whose source,
        dataset or version does not match the report's binding. This blocks
        reports that compare unrelated batches from being committed.
        """
        primary_batch = self._session.get(IngestionBatchRow, str(report.primary_batch_id))
        fallback_batch = self._session.get(IngestionBatchRow, str(report.fallback_batch_id))
        if primary_batch is None:
            raise InvalidDataTransitionError(
                "difference report primary_batch_id does not exist",
                context={"primary_batch_id": str(report.primary_batch_id)},
            )
        if fallback_batch is None:
            raise InvalidDataTransitionError(
                "difference report fallback_batch_id does not exist",
                context={"fallback_batch_id": str(report.fallback_batch_id)},
            )
        if report.primary_version_id == report.fallback_version_id:
            for side, batch, declared in (
                ("primary", primary_batch, verified.primary_empty_result),
                ("fallback", fallback_batch, verified.fallback_empty_result),
            ):
                actual_empty = _load_empty_result(batch.empty_result_json)
                actual = (
                    None
                    if actual_empty is None
                    else {
                        "classification": actual_empty.classification.value,
                        "reason": actual_empty.reason,
                    }
                )
                if declared != actual:
                    raise InvalidDataTransitionError(
                        f"difference report {side} empty result does not match batch"
                    )
        if primary_batch.source != report.primary_source.value:
            raise InvalidDataTransitionError(
                "difference report primary_source does not match primary batch source",
                context={
                    "primary_source": report.primary_source.value,
                    "batch_source": primary_batch.source,
                },
            )
        if fallback_batch.source != report.fallback_source.value:
            raise InvalidDataTransitionError(
                "difference report fallback_source does not match fallback batch source",
                context={
                    "fallback_source": report.fallback_source.value,
                    "batch_source": fallback_batch.source,
                },
            )
        if primary_batch.dataset != fallback_batch.dataset:
            raise InvalidDataTransitionError(
                "difference report primary and fallback batches must share the same dataset",
                context={
                    "primary_dataset": primary_batch.dataset,
                    "fallback_dataset": fallback_batch.dataset,
                },
            )
        primary_version = self._session.get(DatasetVersionRow, str(report.primary_version_id))
        fallback_version = self._session.get(DatasetVersionRow, str(report.fallback_version_id))
        if primary_version is None:
            raise InvalidDataTransitionError(
                "difference report primary_version_id does not exist",
                context={"primary_version_id": str(report.primary_version_id)},
            )
        if fallback_version is None:
            raise InvalidDataTransitionError(
                "difference report fallback_version_id does not exist",
                context={"fallback_version_id": str(report.fallback_version_id)},
            )
        shared_candidate = report.primary_version_id == report.fallback_version_id
        if shared_candidate:
            if primary_version.status not in {
                DatasetVersionStatus.BUILDING.value,
                DatasetVersionStatus.FAILED.value,
                DatasetVersionStatus.PUBLISHED.value,
            }:
                raise InvalidDataTransitionError(
                    "K2 difference report shared candidate has an invalid status"
                )
        elif (
            primary_version.status != DatasetVersionStatus.PUBLISHED.value
            or fallback_version.status != DatasetVersionStatus.PUBLISHED.value
        ):
            raise InvalidDataTransitionError("legacy difference report requires PUBLISHED versions")
        if primary_version.dataset != primary_batch.dataset:
            raise InvalidDataTransitionError(
                "difference report primary_version dataset does not match primary batch dataset",
                context={
                    "version_dataset": primary_version.dataset,
                    "batch_dataset": primary_batch.dataset,
                },
            )
        if fallback_version.dataset != fallback_batch.dataset:
            raise InvalidDataTransitionError(
                "difference report fallback_version dataset does not match fallback batch dataset",
                context={
                    "version_dataset": fallback_version.dataset,
                    "batch_dataset": fallback_batch.dataset,
                },
            )
        if report.version_id != report.primary_version_id:
            raise InvalidDataTransitionError("report.version_id must equal primary_version_id")
        primary_lineage = {
            item.batch_id
            for item in self._session.scalars(
                select(DatasetVersionBatchRow).where(
                    DatasetVersionBatchRow.version_id == str(report.primary_version_id)
                )
            )
        }
        fallback_lineage = {
            item.batch_id
            for item in self._session.scalars(
                select(DatasetVersionBatchRow).where(
                    DatasetVersionBatchRow.version_id == str(report.fallback_version_id)
                )
            )
        }
        if str(report.primary_batch_id) not in primary_lineage:
            raise InvalidDataTransitionError("primary version does not contain compared batch")
        if str(report.fallback_batch_id) not in fallback_lineage:
            raise InvalidDataTransitionError("fallback version does not contain compared batch")
        primary_run = self._session.get(QualityRunRow, str(report.primary_quality_run_id))
        fallback_run = self._session.get(QualityRunRow, str(report.fallback_quality_run_id))
        if primary_run is None:
            raise InvalidDataTransitionError(
                "difference report primary_quality_run_id does not exist",
                context={"primary_quality_run_id": str(report.primary_quality_run_id)},
            )
        if fallback_run is None:
            raise InvalidDataTransitionError(
                "difference report fallback_quality_run_id does not exist",
                context={"fallback_quality_run_id": str(report.fallback_quality_run_id)},
            )
        if str(primary_run.version_id) != str(report.primary_version_id):
            raise InvalidDataTransitionError(
                "difference report primary_quality_run_id is not bound to primary_version_id",
                context={
                    "quality_run_version_id": str(primary_run.version_id),
                    "primary_version_id": str(report.primary_version_id),
                },
            )
        if str(fallback_run.version_id) != str(report.fallback_version_id):
            raise InvalidDataTransitionError(
                "difference report fallback_quality_run_id is not bound to fallback_version_id",
                context={
                    "quality_run_version_id": str(fallback_run.version_id),
                    "fallback_version_id": str(report.fallback_version_id),
                },
            )
        if (
            primary_run.status != QualityRunStatus.PASSED
            or fallback_run.status != QualityRunStatus.PASSED
        ):
            raise InvalidDataTransitionError("difference report requires PASSED quality runs")
        if primary_run.content_sha256 != report.primary_content_sha256:
            raise InvalidDataTransitionError("primary quality SHA does not match report")
        if fallback_run.content_sha256 != report.fallback_content_sha256:
            raise InvalidDataTransitionError("fallback quality SHA does not match report")
        if not shared_candidate:
            if primary_version.content_sha256 != report.primary_content_sha256:
                raise InvalidDataTransitionError(
                    "primary version content SHA does not match report"
                )
            if fallback_version.content_sha256 != report.fallback_content_sha256:
                raise InvalidDataTransitionError(
                    "fallback version content SHA does not match report"
                )
            if primary_version.quality_run_id != str(report.primary_quality_run_id):
                raise InvalidDataTransitionError(
                    "primary version quality run does not match report"
                )
            if fallback_version.quality_run_id != str(report.fallback_quality_run_id):
                raise InvalidDataTransitionError(
                    "fallback version quality run does not match report"
                )

    def get_difference_report(self, report_id: str) -> DifferenceReport:
        row = self._session.get(DifferenceReportRow, report_id)
        if row is None:
            raise InvalidIdError(
                "difference report does not exist", context={"report_id": report_id}
            )
        return _difference_domain(row)

    def list_difference_reports(
        self,
        *,
        version_id: str | None = None,
        primary_source: DataUpstream | None = None,
    ) -> list[DifferenceReport]:
        statement = select(DifferenceReportRow).order_by(
            DifferenceReportRow.created_at, DifferenceReportRow.report_id
        )
        if version_id is not None:
            statement = statement.where(DifferenceReportRow.version_id == version_id)
        if primary_source is not None:
            statement = statement.where(DifferenceReportRow.primary_source == primary_source.value)
        return [_difference_domain(row) for row in self._session.scalars(statement)]

    def list_difference_reports_page(
        self,
        *,
        version_id: str | None,
        primary_source: DataUpstream | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[DifferenceReport]:
        statement = select(DifferenceReportRow)
        if version_id is not None:
            statement = statement.where(DifferenceReportRow.version_id == version_id)
        if primary_source is not None:
            statement = statement.where(DifferenceReportRow.primary_source == primary_source.value)
        if before is not None:
            timestamp, stable_id = before
            statement = statement.where(
                or_(
                    DifferenceReportRow.created_at < timestamp,
                    and_(
                        DifferenceReportRow.created_at == timestamp,
                        DifferenceReportRow.report_id < stable_id,
                    ),
                )
            )
        statement = statement.order_by(
            DifferenceReportRow.created_at.desc(), DifferenceReportRow.report_id.desc()
        ).limit(limit)
        return [_difference_domain(row) for row in self._session.scalars(statement)]

    # -- resolution runs ---------------------------------------------------

    def save_resolution_run(
        self,
        run: ResolutionRun,
        artifact: VerifiedResolutionArtifact,
    ) -> ResolutionRunRecord:
        values = _resolution_row_values(run, artifact)
        existing = self._session.get(ResolutionRunRow, str(run.run_id))
        if existing is not None:
            if not _row_matches(existing, values):
                raise InvalidDataTransitionError("resolution run is immutable")
            return _resolution_domain(existing)
        existing_job = self._session.scalar(
            select(ResolutionRunRow).where(
                ResolutionRunRow.created_job_id == str(run.created_job_id)
            )
        )
        if existing_job is not None:
            raise InvalidDataTransitionError("job already has a resolution run")
        row = ResolutionRunRow(**values)
        self._session.add(row)
        self._session.flush()
        return _resolution_domain(row)

    def get_resolution_run(self, run_id: str) -> ResolutionRunRecord:
        row = self._session.get(ResolutionRunRow, run_id)
        if row is None:
            raise InvalidIdError(
                "resolution run does not exist",
                context={"run_id": run_id},
            )
        return _resolution_domain(row)

    def find_resolution_run_by_fingerprint(
        self,
        input_fingerprint: str,
    ) -> ResolutionRunRecord | None:
        row = self._session.scalar(
            select(ResolutionRunRow)
            .where(ResolutionRunRow.input_fingerprint == input_fingerprint)
            .order_by(ResolutionRunRow.created_at.desc(), ResolutionRunRow.run_id.desc())
        )
        return None if row is None else _resolution_domain(row)

    def find_resolution_run_by_job_id(self, job_id: str) -> ResolutionRunRecord | None:
        row = self._session.scalar(
            select(ResolutionRunRow).where(ResolutionRunRow.created_job_id == job_id)
        )
        return None if row is None else _resolution_domain(row)

    def find_resolution_runs_by_candidate_version_id(
        self, version_id: str
    ) -> list[ResolutionRunRecord]:
        rows = self._session.scalars(
            select(ResolutionRunRow)
            .where(ResolutionRunRow.candidate_version_id == version_id)
            .order_by(ResolutionRunRow.created_at, ResolutionRunRow.run_id)
            .limit(2)
        )
        return [_resolution_domain(row) for row in rows]

    def list_resolution_runs_page(
        self,
        *,
        before: tuple[str, str] | None,
        limit: int,
        conflicts_only: bool = False,
    ) -> list[ResolutionRunRecord]:
        statement = select(ResolutionRunRow)
        if conflicts_only:
            statement = statement.where(ResolutionRunRow.conflict_count > 0)
        if before is not None:
            timestamp, stable_id = before
            statement = statement.where(
                or_(
                    ResolutionRunRow.created_at < timestamp,
                    and_(
                        ResolutionRunRow.created_at == timestamp,
                        ResolutionRunRow.run_id < stable_id,
                    ),
                )
            )
        statement = statement.order_by(
            ResolutionRunRow.created_at.desc(),
            ResolutionRunRow.run_id.desc(),
        ).limit(limit)
        return [_resolution_domain(row) for row in self._session.scalars(statement)]

    # -- audit (delegates to the shared audit_records table) --------------

    def append_audit(
        self,
        *,
        actor: str,
        entity: str,
        action: str,
        correlation_id: str,
        occurred_at: UtcTimestamp,
        details: Mapping[str, str],
    ) -> None:
        """Append a generic audit row to the shared ``audit_records`` table."""
        row = AuditRecordRow(
            actor=actor,
            entity=entity,
            action=action,
            correlation_id=correlation_id,
            occurred_at=occurred_at.to_iso(),
            details_json=_json_mapping(details),
        )
        self._session.add(row)
        self._session.flush()

    def list_audits(self, *, action: str | None, limit: int) -> list[AuditRecord]:
        """List audit records, optionally filtered by action."""
        statement = select(AuditRecordRow).order_by(AuditRecordRow.audit_id).limit(limit)
        if action is not None:
            statement = statement.where(AuditRecordRow.action == action)
        return [
            AuditRecord(
                audit_id=row.audit_id,
                actor=row.actor,
                entity=row.entity,
                action=row.action,
                correlation_id=row.correlation_id,
                occurred_at=UtcTimestamp.from_iso(row.occurred_at),
                details=_load_mapping(row.details_json),
            )
            for row in self._session.scalars(statement)
        ]


# ---------------------------------------------------------------------------
# Unit of work
# ---------------------------------------------------------------------------


class DataSqliteUnitOfWork:
    """Fresh session per call; exceptions always roll back."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self.data: DataRepository

    def __enter__(self) -> Self:
        self._session = self._session_factory()
        self.data = DataRepository(self._session)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._session is None:
            return
        if exc_type is not None:
            self._session.rollback()
        self._session.close()
        self._session = None

    def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        self._session.commit()

    def rollback(self) -> None:
        if self._session is not None:
            self._session.rollback()


class DataSqliteUnitOfWorkFactory:
    """Callable unit-of-work factory safe to share within one process.

    ``execute`` delegates to the exact retry boundary shared with the jobs
    adapter (``execute_with_retry``): bounded exponential backoff with jitter
    for replay-safe ``SQLITE_BUSY``/``SQLITE_LOCKED`` short transactions and a
    fail-closed :class:`PersistenceBusyError` after exhaustion. The sleeper
    and jitter are injectable so retry behavior stays deterministic in tests.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        self._sleeper = sleeper
        self._jitter = jitter

    def __call__(self) -> DataSqliteUnitOfWork:
        return DataSqliteUnitOfWork(self._session_factory)

    def execute(
        self,
        operation: Callable[[DataRepository], ResultT],
        policy: RetryPolicy,
    ) -> ResultT:
        """Run a replay-safe data operation in a fresh session per attempt.

        The operation must be a pure database short transaction: each attempt
        re-executes it against a fresh session, so it must not carry
        filesystem, network or provider side effects.
        """
        return execute_with_retry(
            open_uow=self,
            operation=lambda unit_of_work: operation(unit_of_work.data),
            commit=lambda unit_of_work: unit_of_work.commit(),
            policy=policy,
            sleeper=self._sleeper,
            jitter=self._jitter,
        )
