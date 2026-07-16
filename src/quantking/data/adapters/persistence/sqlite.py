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
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from quantking.common.errors import (
    DatasetVersionImmutableError,
    DatasetVersionNotFoundError,
    InvalidDataTransitionError,
    InvalidIdError,
    OverrideNotApplicableError,
)
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.persistence.models import (
    DatasetVersionRow,
    IngestionBatchRow,
    SourceOverrideRow,
)
from quantking.data.models import (
    BatchFailureInfo,
    DatasetKind,
    DatasetVersion,
    DatasetVersionStatus,
    DataSource,
    IngestionBatch,
    IngestionBatchStatus,
    ManifestFileEntry,
    SourceOverride,
    SourceOverrideStatus,
)
from quantking.jobs.adapters.persistence.models import AuditRecordRow
from quantking.jobs.adapters.persistence.sqlite import (
    create_sqlite_engine as _create_jobs_engine,
)
from quantking.jobs.models import AuditRecord

_IMMUTABLE_VERSION_STATUSES = frozenset({DatasetVersionStatus.PUBLISHED})
_IMMUTABLE_OVERRIDE_STATUSES = frozenset({SourceOverrideStatus.APPLIED})
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
        {"error_type": value.error_type, "message": value.message, "retryable": value.retryable},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _load_failure(value: str | None) -> BatchFailureInfo | None:
    if value is None:
        return None
    loaded = json.loads(value)
    return BatchFailureInfo(
        error_type=str(loaded["error_type"]),
        message=str(loaded["message"]),
        retryable=bool(loaded["retryable"]),
    )


def _timestamp(value: str | None) -> UtcTimestamp | None:
    return None if value is None else UtcTimestamp.from_iso(value)


def _sha256_of_json(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
        "source": batch.source.value,
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
        source=DataSource(row.source),
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
        "failure_json": _failure_json(version.failure),
    }


def _version_domain(row: DatasetVersionRow) -> DatasetVersion:
    from quantking.common.ids import parse_entity_id

    raw_ids = json.loads(row.input_batch_ids_json)
    input_ids = tuple(parse_entity_id(str(item)) for item in raw_ids)
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
        failure=_load_failure(row.failure_json),
    )


# ---------------------------------------------------------------------------
# Override mapping
# ---------------------------------------------------------------------------


def _override_row_values(override: SourceOverride) -> dict[str, object]:
    return {
        "override_id": str(override.override_id),
        "dataset": override.dataset.value,
        "primary_source": override.primary_source.value,
        "fallback_source": override.fallback_source.value,
        "status": override.status.value,
        "scope_codes_json": _json_sequence(override.scope_codes),
        "scope_dates_json": _json_sequence(tuple(str(d) for d in override.scope_dates)),
        "fields_json": _json_sequence(override.fields),
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
    from quantking.common.ids import parse_entity_id

    raw_dates = json.loads(row.scope_dates_json)
    scope_dates = tuple(TradingDate.from_iso(str(d)) for d in raw_dates)
    applied_version = (
        None if row.applied_version_id is None else parse_entity_id(row.applied_version_id)
    )
    return SourceOverride(
        override_id=parse_entity_id(row.override_id),
        dataset=DatasetKind(row.dataset),
        primary_source=DataSource(row.primary_source),
        fallback_source=DataSource(row.fallback_source),
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
        if existing is None:
            self._session.add(IngestionBatchRow(**_batch_row_values(batch)))
        else:
            if existing.status != batch.status.value and existing.status in {
                s.value for s in _TERMINAL_BATCH_STATUSES
            }:
                raise InvalidDataTransitionError(
                    "terminal ingestion batch cannot be mutated; retry must create a new id",
                    context={"batch_id": str(batch.batch_id), "current": existing.status},
                )
            for key, value in _batch_row_values(batch).items():
                setattr(existing, key, value)
        self._session.flush()
        return batch

    def get_batch(self, batch_id: str) -> IngestionBatch:
        row = self._session.get(IngestionBatchRow, batch_id)
        if row is None:
            raise InvalidIdError("ingestion batch does not exist", context={"batch_id": batch_id})
        return _batch_domain(row)

    # -- versions ----------------------------------------------------------

    def save_version(self, version: DatasetVersion) -> DatasetVersion:
        """Insert or update a dataset version row with immutability guards."""
        existing = self._session.get(DatasetVersionRow, str(version.version_id))
        if existing is not None:
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
            for key, value in _version_row_values(version).items():
                setattr(existing, key, value)
        else:
            self._session.add(DatasetVersionRow(**_version_row_values(version)))
        self._session.flush()
        return version

    def get_version(self, version_id: str) -> DatasetVersion:
        row = self._session.get(DatasetVersionRow, version_id)
        if row is None:
            raise DatasetVersionNotFoundError(
                "dataset version does not exist", context={"version_id": version_id}
            )
        return _version_domain(row)

    # -- overrides ---------------------------------------------------------

    def save_override(self, override: SourceOverride) -> SourceOverride:
        """Insert or update a source override row with immutability guards.

        Transitioning PENDING → APPROVED is allowed; mutating a row that is
        already APPROVED or APPLIED is rejected at the service layer (the
        database trigger enforces the same rule for any direct UPDATE/DELETE).
        """
        existing = self._session.get(SourceOverrideRow, str(override.override_id))
        if existing is not None:
            if existing.status in {s.value for s in _IMMUTABLE_OVERRIDE_STATUSES}:
                raise OverrideNotApplicableError(
                    "applied or approved source override is immutable",
                    context={"override_id": str(override.override_id)},
                )
            for key, value in _override_row_values(override).items():
                setattr(existing, key, value)
        else:
            self._session.add(SourceOverrideRow(**_override_row_values(override)))
        self._session.flush()
        return override

    def get_override(self, override_id: str) -> SourceOverride:
        row = self._session.get(SourceOverrideRow, override_id)
        if row is None:
            raise InvalidIdError(
                "source override does not exist", context={"override_id": override_id}
            )
        return _override_domain(row)

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
