"""SQLAlchemy repository and unit-of-work for Stage 1 data metadata.

This adapter maps the framework-free domain objects in
:mod:`quantking.data.models` to and from the ORM rows in
:mod:`quantking.data.adapters.persistence.models`. It reuses the engine and
pragmas from the jobs persistence adapter (``create_sqlite_engine``) but does
not depend on jobs domain logic. Immutability of PUBLISHED versions and
APPLIED/APPROVED overrides is enforced both at the service layer (raise before
write) and at the database layer (triggers created by the Alembic revision).

Short transactions, one fresh session per call; ``SQLITE_BUSY``/``SQLITE_LOCKED``
retries are delegated to the same retry boundary used by jobs. No Parquet row
data is ever written here — only summaries, counts, managed report identifiers
and manifest SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Self

from sqlalchemy import Engine, and_, delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from quantking.common.errors import (
    DatasetVersionImmutableError,
    DatasetVersionNotFoundError,
    InvalidDataTransitionError,
    InvalidIdError,
    OverrideNotApplicableError,
)
from quantking.common.ids import EntityId, new_entity_id, parse_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.persistence.models import (
    DatasetVersionBatchRow,
    DatasetVersionRow,
    DifferenceReportRow,
    IngestionBatchRow,
    QualityResultRow,
    QualityRunRow,
    SourceOverrideApplicationAttemptRow,
    SourceOverrideApplicationRow,
    SourceOverrideRow,
)
from quantking.data.adapters.warehouse.manifest import compute_sha256
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
    SourceOverride,
    SourceOverrideStatus,
    transition_override,
)
from quantking.jobs.adapters.persistence.models import AuditRecordRow
from quantking.jobs.adapters.persistence.sqlite import (
    create_sqlite_engine as _create_jobs_engine,
)
from quantking.jobs.models import AuditRecord

_IMMUTABLE_VERSION_STATUSES = frozenset({DatasetVersionStatus.PUBLISHED})
_TERMINAL_BATCH_STATUSES = frozenset(
    {
        IngestionBatchStatus.SUCCEEDED,
        IngestionBatchStatus.PARTIAL,
        IngestionBatchStatus.FAILED,
    }
)
_ABSOLUTE_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s,;]*|/[A-Za-z0-9_./=-]+")
_TOKEN_RE = re.compile(r"(?i)(token|password|secret|api[_-]?key)=[^\s,;]+")


@dataclass(frozen=True, slots=True)
class OverrideApplicationAttempt:
    attempt_id: EntityId
    override_id: EntityId
    attempt_no: int
    child_version_id: EntityId
    input_fingerprint: str
    status: str
    claimed_by: str
    started_at: UtcTimestamp
    ended_at: UtcTimestamp | None
    failure_message: str | None


@dataclass(frozen=True, slots=True)
class OverrideApplicationFailureState:
    override_status: SourceOverrideStatus
    child_status: DatasetVersionStatus
    failure_message: str


@dataclass(frozen=True, slots=True)
class OverrideApplicationBinding:
    application_id: EntityId
    override_id: EntityId
    child_version_id: EntityId
    input_fingerprint: str
    status: str
    report_sha256: str | None
    contribution_scope_sha256: str | None
    claimed_by: str
    claimed_at: UtcTimestamp
    updated_at: UtcTimestamp
    failure_message: str | None


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


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _sanitize_failure_text(value: str | None) -> str:
    if not value:
        return "application failed"
    redacted = _TOKEN_RE.sub("[REDACTED_SECRET]", value)
    redacted = _ABSOLUTE_PATH_RE.sub("[REDACTED_PATH]", redacted)
    return "".join(ch if ch >= " " or ch in " \t" else "" for ch in redacted)


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
    return IngestionBatch(
        batch_id=parse_entity_id(row.batch_id),
        provider=DataProvider(_load_mapping(row.request_params_json)["provider"]),
        upstream=DataUpstream(row.source),
        endpoint=_load_mapping(row.request_params_json)["endpoint"],
        library_version=_load_mapping(row.request_params_json)["akshare_version"],
        dataset=DatasetKind(row.dataset),
        status=IngestionBatchStatus(row.status),
        adapter_version=row.adapter_version,
        request_params=_load_mapping(row.request_params_json),
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


# ---------------------------------------------------------------------------
# Override mapping
# ---------------------------------------------------------------------------


def _override_row_values(override: SourceOverride) -> dict[str, object]:
    return {
        "override_id": str(override.override_id),
        "dataset": override.dataset.value,
        "difference_report_id": str(override.difference_report_id),
        "primary_batch_id": str(override.primary_batch_id),
        "fallback_batch_id": str(override.fallback_batch_id),
        "primary_source": override.primary_source.value,
        "fallback_source": override.fallback_source.value,
        "status": override.status.value,
        "scope_codes_json": _json_sequence(override.scope_codes),
        "scope_dates_json": _json_sequence(tuple(str(d) for d in override.scope_dates)),
        "fields_json": _json_sequence(override.fields),
        "field_replacements_json": json.dumps(
            [dict(unit) for unit in override.field_replacements],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "fallback_only_keys_json": json.dumps(
            [dict(unit) for unit in override.fallback_only_keys],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "exact_scope_sha256": override.exact_scope_sha256,
        "contribution_scope_sha256": override.contribution_scope_sha256,
        "reason": override.reason,
        "created_at": override.created_at.to_iso(),
        "approved_at": None if override.approved_at is None else override.approved_at.to_iso(),
        "approved_by": override.approved_by,
        "applied_at": None if override.applied_at is None else override.applied_at.to_iso(),
        "applied_version_id": (
            None if override.applied_version_id is None else str(override.applied_version_id)
        ),
    }


def _override_domain(row: SourceOverrideRow) -> SourceOverride:
    raw_dates = json.loads(row.scope_dates_json)
    scope_dates = tuple(TradingDate.from_iso(str(d)) for d in raw_dates)
    applied_version = (
        None if row.applied_version_id is None else parse_entity_id(row.applied_version_id)
    )
    override = SourceOverride(
        override_id=parse_entity_id(row.override_id),
        dataset=DatasetKind(row.dataset),
        difference_report_id=parse_entity_id(row.difference_report_id),
        primary_batch_id=parse_entity_id(row.primary_batch_id),
        fallback_batch_id=parse_entity_id(row.fallback_batch_id),
        primary_source=DataUpstream(row.primary_source),
        fallback_source=DataUpstream(row.fallback_source),
        status=SourceOverrideStatus(row.status),
        scope_codes=_load_str_sequence(row.scope_codes_json),
        scope_dates=scope_dates,
        fields=_load_str_sequence(row.fields_json),
        reason=row.reason,
        created_at=UtcTimestamp.from_iso(row.created_at),
        approved_at=_timestamp(row.approved_at),
        approved_by=row.approved_by,
        applied_at=_timestamp(row.applied_at),
        applied_version_id=applied_version,
        field_replacements=tuple(
            {str(key): str(value) for key, value in unit.items()}
            for unit in json.loads(row.field_replacements_json)
        ),
        fallback_only_keys=tuple(
            {str(key): str(value) for key, value in unit.items()}
            for unit in json.loads(row.fallback_only_keys_json)
        ),
    )
    if row.exact_scope_sha256 != override.exact_scope_sha256:
        raise OverrideNotApplicableError("persisted exact scope SHA-256 is inconsistent")
    if row.contribution_scope_sha256 != override.contribution_scope_sha256:
        raise OverrideNotApplicableError("persisted contribution scope SHA-256 is inconsistent")
    return override


def _override_attempt_domain(
    row: SourceOverrideApplicationAttemptRow,
) -> OverrideApplicationAttempt:
    return OverrideApplicationAttempt(
        attempt_id=parse_entity_id(row.attempt_id),
        override_id=parse_entity_id(row.override_id),
        attempt_no=row.attempt_no,
        child_version_id=parse_entity_id(row.child_version_id),
        input_fingerprint=row.input_fingerprint,
        status=row.status,
        claimed_by=row.claimed_by,
        started_at=UtcTimestamp.from_iso(row.started_at),
        ended_at=_timestamp(row.ended_at),
        failure_message=row.failure_message,
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
        self._validate_difference_report_bindings(domain)
        row = self._session.get(DifferenceReportRow, str(domain.report_id))
        values = _difference_row_values(domain)
        if row is None:
            self._session.add(DifferenceReportRow(**values))
        elif not _row_matches(row, values):
            raise InvalidDataTransitionError("difference reports are immutable")
        self._session.flush()
        return domain

    def _validate_difference_report_bindings(self, report: DifferenceReport) -> None:
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
        if (
            primary_version.status != DatasetVersionStatus.PUBLISHED.value
            or fallback_version.status != DatasetVersionStatus.PUBLISHED.value
        ):
            raise InvalidDataTransitionError("difference report requires PUBLISHED versions")
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
        if primary_version.content_sha256 != report.primary_content_sha256:
            raise InvalidDataTransitionError("primary version content SHA does not match report")
        if fallback_version.content_sha256 != report.fallback_content_sha256:
            raise InvalidDataTransitionError("fallback version content SHA does not match report")
        if primary_version.quality_run_id != str(report.primary_quality_run_id):
            raise InvalidDataTransitionError("primary version quality run does not match report")
        if fallback_version.quality_run_id != str(report.fallback_quality_run_id):
            raise InvalidDataTransitionError("fallback version quality run does not match report")

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

    # -- overrides ---------------------------------------------------------

    def save_override(self, override: SourceOverride) -> SourceOverride:
        """Insert a PENDING override; decisions require audited methods below."""
        existing = self._session.get(SourceOverrideRow, str(override.override_id))
        values = _override_row_values(override)
        if existing is not None:
            if _row_matches(existing, values):
                return override
            raise OverrideNotApplicableError(
                "override decisions must use audited repository operations",
                context={"override_id": str(override.override_id)},
            )
        else:
            if override.status is not SourceOverrideStatus.PENDING:
                raise OverrideNotApplicableError("new override must start in PENDING")
            self._validate_override_bindings(override)
            self._session.add(SourceOverrideRow(**values))
        self._session.flush()
        return override

    def _validate_override_bindings(self, override: SourceOverride) -> None:
        report_row = self._session.get(DifferenceReportRow, str(override.difference_report_id))
        primary = self._session.get(IngestionBatchRow, str(override.primary_batch_id))
        fallback = self._session.get(IngestionBatchRow, str(override.fallback_batch_id))
        if report_row is None or primary is None or fallback is None:
            raise OverrideNotApplicableError("override lineage references must exist")
        report_version = self._session.get(DatasetVersionRow, report_row.version_id)
        if report_version is None or report_version.dataset != override.dataset.value:
            raise OverrideNotApplicableError("difference report dataset does not match override")
        if (
            report_row.primary_source != override.primary_source.value
            or report_row.fallback_source != override.fallback_source.value
            or primary.source != override.primary_source.value
            or fallback.source != override.fallback_source.value
            or primary.dataset != override.dataset.value
            or fallback.dataset != override.dataset.value
            or primary.status not in {"SUCCEEDED", "PARTIAL"}
            or fallback.status not in {"SUCCEEDED", "PARTIAL"}
        ):
            raise OverrideNotApplicableError("override source or batch lineage is inconsistent")
        # The override's primary/fallback batches must match the difference
        # report's bindings; an override cannot be bound to a report that
        # compared different batches.
        if str(report_row.primary_batch_id) != str(override.primary_batch_id) or str(
            report_row.fallback_batch_id
        ) != str(override.fallback_batch_id):
            raise OverrideNotApplicableError(
                "override batches must match the difference report's compared batches"
            )

    def approve_override(self, override_id: str, *, actor: str, at: UtcTimestamp) -> SourceOverride:
        return self._decide_override(
            override_id,
            target=SourceOverrideStatus.APPROVED,
            actor=actor,
            at=at,
            applied_version_id=None,
        )

    def reject_override(self, override_id: str, *, actor: str, at: UtcTimestamp) -> SourceOverride:
        return self._decide_override(
            override_id,
            target=SourceOverrideStatus.REJECTED,
            actor=actor,
            at=at,
            applied_version_id=None,
        )

    def apply_override(
        self,
        override_id: str,
        *,
        actor: str,
        at: UtcTimestamp,
        applied_version_id: str,
    ) -> SourceOverride:
        from quantking.common.ids import parse_entity_id

        return self._decide_override(
            override_id,
            target=SourceOverrideStatus.APPLIED,
            actor=actor,
            at=at,
            applied_version_id=parse_entity_id(applied_version_id),
        )

    def _decide_override(
        self,
        override_id: str,
        *,
        target: SourceOverrideStatus,
        actor: str,
        at: UtcTimestamp,
        applied_version_id: object,
    ) -> SourceOverride:
        from quantking.common.ids import EntityId

        row = self._session.get(SourceOverrideRow, override_id)
        if row is None:
            raise InvalidIdError(
                "source override does not exist", context={"override_id": override_id}
            )
        current = _override_domain(row)
        version_id = applied_version_id if isinstance(applied_version_id, EntityId) else None
        if target is SourceOverrideStatus.APPLIED:
            if version_id is None:
                raise OverrideNotApplicableError("APPLIED requires a dataset version")
            version = self.get_version(str(version_id))
            report = self.get_difference_report(str(current.difference_report_id))
            if (
                version.dataset is not current.dataset
                or version.status is not DatasetVersionStatus.PUBLISHED
                or version.version_id == report.version_id
                or version.parent_version_id != report.version_id
                or current.approved_at is None
                or version.created_at.value < current.approved_at.value
            ):
                raise OverrideNotApplicableError(
                    "applied version must be a new same-dataset child of the compared version"
                )
            application = self._session.scalar(
                select(SourceOverrideApplicationRow).where(
                    SourceOverrideApplicationRow.override_id == override_id,
                    SourceOverrideApplicationRow.child_version_id == str(version_id),
                    SourceOverrideApplicationRow.status == "PUBLISHED",
                )
            )
            if application is None:
                raise OverrideNotApplicableError(
                    "APPLIED requires the persisted PUBLISHED application binding"
                )
        changed = transition_override(
            current,
            target,
            at=at,
            actor=actor,
            applied_version_id=version_id,
        )
        for key, value in _override_row_values(changed).items():
            setattr(row, key, value)
        if target is SourceOverrideStatus.APPLIED:
            self._session.flush()
            assert version_id is not None
            assert application is not None
            application.status = "APPLIED"
            application.updated_at = at.to_iso()
            attempt = self._session.scalar(
                select(SourceOverrideApplicationAttemptRow).where(
                    SourceOverrideApplicationAttemptRow.override_id == override_id,
                    SourceOverrideApplicationAttemptRow.child_version_id == str(version_id),
                )
            )
            if attempt is None or attempt.status != "PUBLISHED":
                raise OverrideNotApplicableError(
                    "APPLIED requires the persisted PUBLISHED application attempt"
                )
            attempt.status = "APPLIED"
        action = {
            SourceOverrideStatus.APPROVED: "override_approved",
            SourceOverrideStatus.REJECTED: "override_rejected",
            SourceOverrideStatus.APPLIED: "override_applied",
        }[target]
        details = {"status": target.value}
        if version_id is not None:
            details["applied_version_id"] = str(version_id)
        self.append_audit(
            actor=actor,
            entity=override_id,
            action=action,
            correlation_id=override_id,
            occurred_at=at,
            details=details,
        )
        self._session.flush()
        return changed

    def get_override(self, override_id: str) -> SourceOverride:
        row = self._session.get(SourceOverrideRow, override_id)
        if row is None:
            raise InvalidIdError(
                "source override does not exist", context={"override_id": override_id}
            )
        return _override_domain(row)

    def list_overrides_page(
        self,
        *,
        dataset: DatasetKind | None,
        status: SourceOverrideStatus | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[SourceOverride]:
        statement = select(SourceOverrideRow)
        if dataset is not None:
            statement = statement.where(SourceOverrideRow.dataset == dataset.value)
        if status is not None:
            statement = statement.where(SourceOverrideRow.status == status.value)
        if before is not None:
            timestamp, stable_id = before
            statement = statement.where(
                or_(
                    SourceOverrideRow.created_at < timestamp,
                    and_(
                        SourceOverrideRow.created_at == timestamp,
                        SourceOverrideRow.override_id < stable_id,
                    ),
                )
            )
        statement = statement.order_by(
            SourceOverrideRow.created_at.desc(), SourceOverrideRow.override_id.desc()
        ).limit(limit)
        return [_override_domain(row) for row in self._session.scalars(statement)]

    # -- override application attempts -------------------------------------

    def _validate_override_application_graph(
        self,
        *,
        override_id: str,
        child_version_id: str,
        allow_applied: bool = False,
    ) -> tuple[SourceOverride, DifferenceReport, DatasetVersion]:
        override = self.get_override(override_id)
        allowed = {SourceOverrideStatus.APPROVED}
        if allow_applied:
            allowed.add(SourceOverrideStatus.APPLIED)
        if override.status not in allowed or override.approved_at is None:
            raise OverrideNotApplicableError("override application requires an APPROVED override")
        report = self.get_difference_report(str(override.difference_report_id))
        child = self.get_version(child_version_id)
        if (
            child.dataset is not override.dataset
            or child.parent_version_id != report.primary_version_id
            or child.input_batch_ids != (override.primary_batch_id, override.fallback_batch_id)
            or child.created_at.value < override.approved_at.value
        ):
            raise OverrideNotApplicableError(
                "override application child has invalid dataset, parent or batch lineage"
            )
        return override, report, child

    def claim_override_application_child(
        self,
        *,
        override_id: str,
        child: DatasetVersion,
        input_fingerprint: str,
        actor: str,
        at: UtcTimestamp,
    ) -> OverrideApplicationAttempt:
        """Atomically reuse an active child or persist and claim the supplied child."""
        active = self._session.scalar(
            select(SourceOverrideApplicationRow).where(
                SourceOverrideApplicationRow.override_id == override_id,
                SourceOverrideApplicationRow.status.in_(["CLAIMED", "PUBLISHED", "APPLIED"]),
            )
        )
        if active is not None:
            if active.input_fingerprint != input_fingerprint:
                raise OverrideNotApplicableError(
                    "active override application fingerprint does not match retry input"
                )
            self._validate_override_application_graph(
                override_id=override_id,
                child_version_id=active.child_version_id,
                allow_applied=True,
            )
            attempt = self._session.scalar(
                select(SourceOverrideApplicationAttemptRow).where(
                    SourceOverrideApplicationAttemptRow.override_id == override_id,
                    SourceOverrideApplicationAttemptRow.child_version_id == active.child_version_id,
                )
            )
            if attempt is None:
                raise OverrideNotApplicableError("active application is missing its attempt")
            return _override_attempt_domain(attempt)
        if child.status is not DatasetVersionStatus.BUILDING:
            raise OverrideNotApplicableError("new override application child must be BUILDING")
        self.save_version(child)
        return self.claim_override_application(
            override_id=override_id,
            child_version_id=str(child.version_id),
            input_fingerprint=input_fingerprint,
            actor=actor,
            at=at,
        )

    def claim_override_application(
        self,
        *,
        override_id: str,
        child_version_id: str,
        input_fingerprint: str,
        actor: str,
        at: UtcTimestamp,
    ) -> OverrideApplicationAttempt:
        if not _is_sha256(input_fingerprint):
            raise OverrideNotApplicableError("input_fingerprint must be a SHA-256")
        self._validate_override_application_graph(
            override_id=override_id, child_version_id=child_version_id
        )
        active = self._session.scalar(
            select(SourceOverrideApplicationRow).where(
                SourceOverrideApplicationRow.override_id == override_id,
                SourceOverrideApplicationRow.status.in_(["CLAIMED", "PUBLISHED", "APPLIED"]),
            )
        )
        if active is not None:
            if active.input_fingerprint != input_fingerprint:
                raise OverrideNotApplicableError(
                    "active override application fingerprint does not match retry input"
                )
            attempt = self._session.scalar(
                select(SourceOverrideApplicationAttemptRow).where(
                    SourceOverrideApplicationAttemptRow.override_id == override_id,
                    SourceOverrideApplicationAttemptRow.child_version_id == active.child_version_id,
                )
            )
            if attempt is not None:
                return _override_attempt_domain(attempt)
        max_attempt = self._session.scalar(
            select(SourceOverrideApplicationAttemptRow.attempt_no)
            .where(SourceOverrideApplicationAttemptRow.override_id == override_id)
            .order_by(SourceOverrideApplicationAttemptRow.attempt_no.desc())
            .limit(1)
        )
        attempt_no = int(max_attempt or 0) + 1
        attempt = SourceOverrideApplicationAttemptRow(
            attempt_id=str(new_entity_id("data_override_attempt")),
            override_id=override_id,
            attempt_no=attempt_no,
            child_version_id=child_version_id,
            input_fingerprint=input_fingerprint,
            status="CLAIMED",
            claimed_by=actor,
            started_at=at.to_iso(),
            ended_at=None,
            failure_message=None,
        )
        self._session.add(attempt)
        self._session.add(
            SourceOverrideApplicationRow(
                application_id=str(new_entity_id("data_override_application")),
                override_id=override_id,
                child_version_id=child_version_id,
                input_fingerprint=input_fingerprint,
                status="CLAIMED",
                report_sha256=None,
                contribution_scope_sha256=None,
                claimed_by=actor,
                claimed_at=at.to_iso(),
                updated_at=at.to_iso(),
                failure_message=None,
            )
        )
        self._session.flush()
        return _override_attempt_domain(attempt)

    def finish_override_application_attempt(
        self,
        *,
        attempt_id: str,
        status: str,
        failure_message: str | None,
        at: UtcTimestamp,
    ) -> OverrideApplicationAttempt:
        if status not in {"FAILED", "PUBLISHED", "APPLIED"}:
            raise OverrideNotApplicableError("invalid override application attempt status")
        row = self._session.get(SourceOverrideApplicationAttemptRow, attempt_id)
        if row is None:
            raise InvalidIdError("override application attempt does not exist")
        _, _, child = self._validate_override_application_graph(
            override_id=row.override_id,
            child_version_id=row.child_version_id,
            allow_applied=status == "APPLIED",
        )
        if status == "FAILED" and child.status is not DatasetVersionStatus.FAILED:
            raise OverrideNotApplicableError("FAILED attempt requires a real FAILED child")
        if (
            status in {"PUBLISHED", "APPLIED"}
            and child.status is not DatasetVersionStatus.PUBLISHED
        ):
            raise OverrideNotApplicableError("published attempt requires a real PUBLISHED child")
        row.status = status
        row.ended_at = at.to_iso()
        row.failure_message = _sanitize_failure_text(failure_message) if failure_message else None
        app = self._session.scalar(
            select(SourceOverrideApplicationRow).where(
                SourceOverrideApplicationRow.override_id == row.override_id,
                SourceOverrideApplicationRow.child_version_id == row.child_version_id,
            )
        )
        if app is not None:
            app.status = status
            app.updated_at = at.to_iso()
            app.failure_message = row.failure_message
        self._session.flush()
        return _override_attempt_domain(row)

    def bind_published_override_child(
        self,
        *,
        override_id: str,
        child_version_id: str,
        report_sha256: str,
        contribution_scope_sha256: str,
        manifest_source_override: Mapping[str, object],
        at: UtcTimestamp,
    ) -> OverrideApplicationAttempt:
        if not _is_sha256(report_sha256) or not _is_sha256(contribution_scope_sha256):
            raise OverrideNotApplicableError("override child binding requires SHA-256 credentials")
        override, report, child = self._validate_override_application_graph(
            override_id=override_id, child_version_id=child_version_id
        )
        if child.status is not DatasetVersionStatus.PUBLISHED:
            raise OverrideNotApplicableError("override binding requires a real PUBLISHED child")
        self._validate_override_manifest_provenance(
            override=override,
            report=report,
            provenance=manifest_source_override,
            report_sha256=report_sha256,
            contribution_scope_sha256=contribution_scope_sha256,
        )
        row = self._session.scalar(
            select(SourceOverrideApplicationRow).where(
                SourceOverrideApplicationRow.override_id == override_id,
                SourceOverrideApplicationRow.status.in_(["CLAIMED", "PUBLISHED"]),
            )
        )
        if row is None:
            raise OverrideNotApplicableError("published child has no persisted application claim")
        if row.child_version_id != child_version_id or row.status not in {"CLAIMED", "PUBLISHED"}:
            raise OverrideNotApplicableError("published child does not match the active claim")
        row.status = "PUBLISHED"
        row.report_sha256 = report_sha256
        row.contribution_scope_sha256 = contribution_scope_sha256
        row.updated_at = at.to_iso()
        attempt_row = self._session.scalar(
            select(SourceOverrideApplicationAttemptRow).where(
                SourceOverrideApplicationAttemptRow.override_id == override_id,
                SourceOverrideApplicationAttemptRow.child_version_id == child_version_id,
            )
        )
        if attempt_row is None or attempt_row.status not in {"CLAIMED", "PUBLISHED"}:
            raise OverrideNotApplicableError(
                "published child is missing its active application attempt"
            )
        attempt_row.status = "PUBLISHED"
        attempt_row.ended_at = at.to_iso()
        attempt_row.failure_message = None
        self._session.flush()
        return _override_attempt_domain(attempt_row)

    def _validate_override_manifest_provenance(
        self,
        *,
        override: SourceOverride,
        report: DifferenceReport,
        provenance: Mapping[str, object],
        report_sha256: str,
        contribution_scope_sha256: str,
    ) -> None:
        primary = provenance.get("primary")
        fallback = provenance.get("fallback")
        contributions = provenance.get("fallback_contributions")
        summary = provenance.get("contribution_summary")
        if (
            not isinstance(primary, Mapping)
            or not isinstance(fallback, Mapping)
            or not isinstance(contributions, list)
            or (summary is not None and not isinstance(summary, Mapping))
        ):
            raise OverrideNotApplicableError("manifest source_override provenance is invalid")
        expected_primary = {
            "source": override.primary_source.value,
            "batch_id": str(override.primary_batch_id),
            "version_id": str(report.primary_version_id),
            "content_sha256": report.primary_content_sha256,
        }
        expected_fallback = {
            "source": override.fallback_source.value,
            "batch_id": str(override.fallback_batch_id),
            "version_id": str(report.fallback_version_id),
            "content_sha256": report.fallback_content_sha256,
        }
        digest_payload: object = contributions
        if isinstance(summary, Mapping):
            digest_payload = {"contributions": contributions, "summary": dict(summary)}
        computed_scope_sha = compute_sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        expected_fields = {
            "override_id",
            "difference_report_id",
            "report_sha256",
            "primary",
            "fallback",
            "fallback_contributions",
            "contribution_scope_sha256",
        }
        if summary is not None:
            expected_fields.add("contribution_summary")
        if (
            set(provenance) != expected_fields
            or provenance.get("override_id") != str(override.override_id)
            or provenance.get("difference_report_id") != str(report.report_id)
            or provenance.get("report_sha256") != report.report_sha256
            or report_sha256 != report.report_sha256
            or dict(primary) != expected_primary
            or dict(fallback) != expected_fallback
            or provenance.get("contribution_scope_sha256") != computed_scope_sha
            or contribution_scope_sha256 != computed_scope_sha
        ):
            raise OverrideNotApplicableError(
                "manifest source_override provenance does not match the application lineage"
            )

    def find_recoverable_override_child(self, override_id: str) -> DatasetVersion:
        row = self._session.scalar(
            select(SourceOverrideApplicationRow).where(
                SourceOverrideApplicationRow.override_id == override_id,
                SourceOverrideApplicationRow.status.in_(["CLAIMED", "PUBLISHED", "APPLIED"]),
            )
        )
        if row is None:
            raise InvalidIdError("recoverable override child does not exist")
        _, _, child = self._validate_override_application_graph(
            override_id=override_id,
            child_version_id=row.child_version_id,
            allow_applied=True,
        )
        if (
            row.status in {"PUBLISHED", "APPLIED"}
            and child.status is not DatasetVersionStatus.PUBLISHED
        ):
            raise OverrideNotApplicableError("recoverable published binding has no PUBLISHED child")
        return child

    def get_override_application_binding(self, override_id: str) -> OverrideApplicationBinding:
        row = self._session.scalar(
            select(SourceOverrideApplicationRow).where(
                SourceOverrideApplicationRow.override_id == override_id,
                SourceOverrideApplicationRow.status.in_(["CLAIMED", "PUBLISHED", "APPLIED"]),
            )
        )
        if row is None:
            raise InvalidIdError("override application binding does not exist")
        self._validate_override_application_graph(
            override_id=override_id,
            child_version_id=row.child_version_id,
            allow_applied=True,
        )
        return self._override_binding_domain(row)

    @staticmethod
    def _override_binding_domain(
        row: SourceOverrideApplicationRow,
    ) -> OverrideApplicationBinding:
        return OverrideApplicationBinding(
            application_id=parse_entity_id(row.application_id),
            override_id=parse_entity_id(row.override_id),
            child_version_id=parse_entity_id(row.child_version_id),
            input_fingerprint=row.input_fingerprint,
            status=row.status,
            report_sha256=row.report_sha256,
            contribution_scope_sha256=row.contribution_scope_sha256,
            claimed_by=row.claimed_by,
            claimed_at=UtcTimestamp.from_iso(row.claimed_at),
            updated_at=UtcTimestamp.from_iso(row.updated_at),
            failure_message=row.failure_message,
        )

    def find_latest_override_application_binding(
        self, override_id: str
    ) -> OverrideApplicationBinding | None:
        row = self._session.scalar(
            select(SourceOverrideApplicationRow)
            .where(SourceOverrideApplicationRow.override_id == override_id)
            .order_by(
                SourceOverrideApplicationRow.claimed_at.desc(),
                SourceOverrideApplicationRow.application_id.desc(),
            )
            .limit(1)
        )
        return self._override_binding_domain(row) if row is not None else None

    def find_active_override_application_binding(
        self, override_id: str
    ) -> OverrideApplicationBinding | None:
        row = self._session.scalar(
            select(SourceOverrideApplicationRow).where(
                SourceOverrideApplicationRow.override_id == override_id,
                SourceOverrideApplicationRow.status.in_(["CLAIMED", "PUBLISHED", "APPLIED"]),
            )
        )
        return self._override_binding_domain(row) if row is not None else None

    def find_last_override_application_attempt(
        self, override_id: str
    ) -> OverrideApplicationAttempt | None:
        row = self._session.scalar(
            select(SourceOverrideApplicationAttemptRow)
            .where(SourceOverrideApplicationAttemptRow.override_id == override_id)
            .order_by(SourceOverrideApplicationAttemptRow.attempt_no.desc())
            .limit(1)
        )
        return _override_attempt_domain(row) if row is not None else None

    def list_override_audits(self, override_id: str, *, limit: int) -> list[AuditRecord]:
        rows = self._session.scalars(
            select(AuditRecordRow)
            .where(AuditRecordRow.entity == override_id)
            .order_by(AuditRecordRow.audit_id.desc())
            .limit(limit)
        )
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
            for row in rows
        ]

    def record_override_application_failure(
        self,
        *,
        override_id: str,
        attempt_id: str,
        child_version_id: str,
        failure_stage: str,
        message: str,
        at: UtcTimestamp,
    ) -> OverrideApplicationFailureState:
        sanitized = _sanitize_failure_text(f"{failure_stage}: {message}")
        _, _, child = self._validate_override_application_graph(
            override_id=override_id, child_version_id=child_version_id
        )
        if child.status is not DatasetVersionStatus.FAILED:
            raise OverrideNotApplicableError("failure recording requires a real FAILED child")
        attempt = self._session.get(SourceOverrideApplicationAttemptRow, attempt_id)
        if (
            attempt is None
            or attempt.override_id != override_id
            or attempt.child_version_id != child_version_id
        ):
            raise OverrideNotApplicableError("failure attempt does not match override child")
        self.finish_override_application_attempt(
            attempt_id=attempt_id, status="FAILED", failure_message=sanitized, at=at
        )
        return OverrideApplicationFailureState(
            override_status=SourceOverrideStatus.APPROVED,
            child_status=DatasetVersionStatus.FAILED,
            failure_message=sanitized,
        )

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
    """Callable unit-of-work factory safe to share within one process."""

    def __init__(self, engine: Engine) -> None:
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def __call__(self) -> DataSqliteUnitOfWork:
        return DataSqliteUnitOfWork(self._session_factory)
