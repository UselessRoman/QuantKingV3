"""SQLAlchemy 2 and Alembic adapter for durable SQLite jobs."""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Self, TypeVar

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, and_, create_engine, event, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from quantking.common.errors import (
    IdempotencyConflictError,
    InvalidJobTransitionError,
    InvalidPathError,
    JobConflictError,
    JobNotFoundError,
    PersistenceBusyError,
)
from quantking.common.paths import is_reparse_point
from quantking.common.time import UtcTimestamp
from quantking.jobs.adapters.persistence.models import (
    AuditRecordRow,
    JobDependencyRow,
    JobEventRow,
    JobRunRow,
)
from quantking.jobs.models import (
    AuditRecord,
    FailureInfo,
    JobEvent,
    JobRun,
    JobStatus,
    RetryPolicy,
    transition_job,
)
from quantking.jobs.ports import JobRepository

_REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
ResultT = TypeVar("ResultT")


def _reserve_new_database(database: Path) -> None:
    """Atomically reserve a never-before-existing database at an explicit safe path."""
    if not isinstance(database, Path) or not database.is_absolute():
        raise InvalidPathError(
            "SQLite initialization path must be absolute",
            context={"path": str(database)},
        )
    if ".." in database.parts:
        raise InvalidPathError(
            "SQLite initialization path must not contain parent traversal",
            context={"path": str(database)},
        )

    parent = database.parent
    try:
        for component in (parent, *parent.parents):
            if component.exists() and is_reparse_point(component):
                raise InvalidPathError(
                    "SQLite initialization path cannot contain a symbolic link or junction",
                    context={"path": str(database)},
                )
    except OSError as error:
        raise InvalidPathError(
            "SQLite initialization path could not be inspected safely",
            context={"path": str(database)},
        ) from error

    if not parent.is_dir():
        raise InvalidPathError(
            "SQLite initialization parent must already exist as a directory",
            context={"parent": str(parent)},
        )

    if database.exists() or is_reparse_point(database):
        raise InvalidPathError(
            "SQLite initialization target must not already exist",
            context={"path": str(database)},
        )
    try:
        with database.open("xb"):
            pass
    except FileExistsError as error:
        raise InvalidPathError(
            "SQLite initialization target must not already exist",
            context={"path": str(database)},
        ) from error
    except OSError as error:
        raise InvalidPathError(
            "SQLite initialization target could not be reserved safely",
            context={"path": str(database)},
        ) from error


def create_sqlite_engine(database: Path, *, busy_timeout_ms: int) -> Engine:
    """Create a file engine whose every DB-API connection has required pragmas."""
    if not isinstance(database, Path) or not database.is_absolute():
        raise ValueError("SQLite database path must be absolute")
    if not 1 <= busy_timeout_ms <= 60_000:
        raise ValueError("busy_timeout_ms must be between 1 and 60000")
    database.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite+pysqlite:///{database.as_posix()}",
        poolclass=NullPool,
        connect_args={"timeout": busy_timeout_ms / 1000, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _configure_connection(
        dbapi_connection: sqlite3.Connection,
        connection_record: object,
    ) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms:d}")
        finally:
            cursor.close()

    return engine


def initialize_database(database: Path, *, busy_timeout_ms: int = 250) -> None:
    """Initialize Alembic head only in a brand-new, explicitly safe database file.

    This Stage 0 helper is intentionally not an existing-database migration entry point.
    A future migration of an existing database must require a completed, verified S0-05
    snapshot credential before any database connection is opened.
    """
    if not 1 <= busy_timeout_ms <= 60_000:
        raise ValueError("busy_timeout_ms must be between 1 and 60000")
    _reserve_new_database(database)
    engine = create_sqlite_engine(database, busy_timeout_ms=busy_timeout_ms)
    configuration = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_REPOSITORY_ROOT / "migrations"))
    configuration.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.as_posix()}")
    try:
        with engine.connect() as connection:
            configuration.attributes["connection"] = connection
            command.upgrade(configuration, "head")
    finally:
        engine.dispose()


def _json_mapping(value: Mapping[str, str]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_sequence(value: tuple[str, ...]) -> str:
    return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))


def _load_mapping(value: str) -> dict[str, str]:
    loaded = json.loads(value)
    if not isinstance(loaded, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in loaded.items()
    ):
        raise ValueError("persisted mapping is invalid")
    return loaded


def _load_sequence(value: str) -> tuple[str, ...]:
    loaded = json.loads(value)
    if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
        raise ValueError("persisted sequence is invalid")
    return tuple(loaded)


def _failure_json(value: FailureInfo | None) -> str | None:
    if value is None:
        return None
    return json.dumps(
        {
            "error_type": value.error_type,
            "message": value.message,
            "retryable": value.retryable,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _load_failure(value: str | None) -> FailureInfo | None:
    if value is None:
        return None
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("persisted failure is invalid")
    return FailureInfo(
        error_type=str(loaded["error_type"]),
        message=str(loaded["message"]),
        retryable=bool(loaded["retryable"]),
    )


def _timestamp(value: str | None) -> UtcTimestamp | None:
    return None if value is None else UtcTimestamp.from_iso(value)


def _row_values(job: JobRun) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "idempotency_key": job.idempotency_key,
        "input_manifest_json": _json_mapping(job.input_manifest),
        "config_manifest_json": _json_mapping(job.config_manifest),
        "version_manifest_json": _json_mapping(job.version_manifest),
        "requester": job.requester,
        "status": job.status.value,
        "created_at": job.created_at.to_iso(),
        "started_at": None if job.started_at is None else job.started_at.to_iso(),
        "ended_at": None if job.ended_at is None else job.ended_at.to_iso(),
        "heartbeat_at": None if job.heartbeat_at is None else job.heartbeat_at.to_iso(),
        "stage": job.stage,
        "progress": job.progress,
        "worker_id": job.worker_id,
        "log_id": job.log_id,
        "artifacts_json": _json_sequence(job.artifacts),
        "failure_json": _failure_json(job.failure),
        "cancel_requested_at": (
            None if job.cancel_requested_at is None else job.cancel_requested_at.to_iso()
        ),
        "cancel_request": job.cancel_request,
        "cancel_result": job.cancel_result,
        "non_cancellable_phase": job.non_cancellable_phase,
        "row_version": job.row_version,
    }


class SQLiteJobRepository:
    """Repository scoped to one SQLAlchemy session and one short transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _row(self, job_id: str) -> JobRunRow:
        row = self._session.get(JobRunRow, job_id)
        if row is None:
            raise JobNotFoundError("job does not exist", context={"job_id": job_id})
        return row

    def _domain(self, row: JobRunRow) -> JobRun:
        upstream = tuple(
            self._session.scalars(
                select(JobDependencyRow.upstream_job_id)
                .where(JobDependencyRow.job_id == row.job_id)
                .order_by(JobDependencyRow.upstream_job_id)
            )
        )
        return JobRun(
            job_id=row.job_id,
            job_type=row.job_type,
            idempotency_key=row.idempotency_key,
            input_manifest=_load_mapping(row.input_manifest_json),
            config_manifest=_load_mapping(row.config_manifest_json),
            version_manifest=_load_mapping(row.version_manifest_json),
            requester=row.requester,
            status=JobStatus(row.status),
            created_at=UtcTimestamp.from_iso(row.created_at),
            started_at=_timestamp(row.started_at),
            ended_at=_timestamp(row.ended_at),
            heartbeat_at=_timestamp(row.heartbeat_at),
            stage=row.stage,
            progress=row.progress,
            worker_id=row.worker_id,
            log_id=row.log_id,
            artifacts=_load_sequence(row.artifacts_json),
            failure=_load_failure(row.failure_json),
            cancel_requested_at=_timestamp(row.cancel_requested_at),
            cancel_request=row.cancel_request,
            cancel_result=row.cancel_result,
            non_cancellable_phase=row.non_cancellable_phase,
            row_version=row.row_version,
            upstream_job_ids=upstream,
        )

    def _apply(self, row: JobRunRow, job: JobRun) -> None:
        for key, value in _row_values(job).items():
            setattr(row, key, value)

    def _event(
        self,
        *,
        job_id: str,
        event_type: str,
        stage: str | None,
        progress: float | None,
        summary: str,
        at: UtcTimestamp,
    ) -> JobEventRow:
        row = JobEventRow(
            job_id=job_id,
            event_type=event_type,
            stage=stage,
            progress=progress,
            summary=summary,
            occurred_at=at.to_iso(),
        )
        self._session.add(row)
        self._session.flush()
        return row

    def create_or_get(self, job: JobRun) -> tuple[JobRun, bool]:
        inserted_job_id = self._session.scalar(
            sqlite_insert(JobRunRow)
            .values(**_row_values(job))
            .on_conflict_do_nothing(index_elements=["job_type", "idempotency_key"])
            .returning(JobRunRow.job_id)
        )
        created = inserted_job_id is not None
        if created:
            for upstream_job_id in job.upstream_job_ids:
                self._session.add(
                    JobDependencyRow(job_id=job.job_id, upstream_job_id=upstream_job_id)
                )
            self._event(
                job_id=job.job_id,
                event_type="job_created",
                stage=None,
                progress=0.0,
                summary="job created",
                at=job.created_at,
            )
            return job, True
        row = self._session.scalar(
            select(JobRunRow).where(
                JobRunRow.job_type == job.job_type,
                JobRunRow.idempotency_key == job.idempotency_key,
            )
        )
        if row is None:
            raise RuntimeError("idempotent insert returned no existing job")
        existing = self._domain(row)
        same_command = (
            existing.input_manifest == job.input_manifest
            and existing.config_manifest == job.config_manifest
            and existing.version_manifest == job.version_manifest
            and tuple(sorted(existing.upstream_job_ids)) == tuple(sorted(job.upstream_job_ids))
        )
        if not same_command:
            raise IdempotencyConflictError(
                "idempotency key was reused with different command input",
                context={
                    "job_type": job.job_type,
                    "idempotency_key": job.idempotency_key,
                },
            )
        return existing, False

    def get(self, job_id: str) -> JobRun:
        return self._domain(self._row(job_id))

    def list_page(
        self,
        *,
        job_type: str | None,
        status: JobStatus | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[JobRun]:
        """Return one newest-first database keyset page."""
        statement = select(JobRunRow)
        if job_type is not None:
            statement = statement.where(JobRunRow.job_type == job_type)
        if status is not None:
            statement = statement.where(JobRunRow.status == status.value)
        if before is not None:
            timestamp, stable_id = before
            statement = statement.where(
                or_(
                    JobRunRow.created_at < timestamp,
                    and_(
                        JobRunRow.created_at == timestamp,
                        JobRunRow.job_id < stable_id,
                    ),
                )
            )
        rows = self._session.scalars(
            statement.order_by(JobRunRow.created_at.desc(), JobRunRow.job_id.desc()).limit(limit)
        )
        return [self._domain(row) for row in rows]

    def count_jobs(self, *, job_type: str | None, status: JobStatus | None) -> int:
        """Count jobs matching the same allowlisted filters as list_page."""
        statement = select(func.count()).select_from(JobRunRow)
        if job_type is not None:
            statement = statement.where(JobRunRow.job_type == job_type)
        if status is not None:
            statement = statement.where(JobRunRow.status == status.value)
        return int(self._session.scalar(statement) or 0)

    def transition(
        self,
        job_id: str,
        target: JobStatus,
        *,
        at: UtcTimestamp,
        worker_id: str | None,
        failure: FailureInfo | None,
        cancel_result: str | None,
        summary: str,
    ) -> JobRun:
        row = self._row(job_id)
        changed = transition_job(
            self._domain(row),
            target,
            at=at,
            worker_id=worker_id,
            failure=failure,
            cancel_result=cancel_result,
        )
        changed_values = _row_values(changed)
        changed_values.pop("job_id")
        updated_job_id = self._session.scalar(
            update(JobRunRow)
            .where(
                JobRunRow.job_id == job_id,
                JobRunRow.status == row.status,
                JobRunRow.row_version == row.row_version,
            )
            .values(**changed_values)
            .returning(JobRunRow.job_id)
        )
        if updated_job_id is None:
            raise InvalidJobTransitionError(
                "job changed concurrently before transition commit",
                context={"job_id": job_id, "target": target.value},
            )
        event_name = {
            JobStatus.RUNNING: "job_running",
            JobStatus.SUCCEEDED: "job_succeeded",
            JobStatus.FAILED: "job_failed",
            JobStatus.CANCELLED: "job_cancelled",
            JobStatus.INTERRUPTED: "job_interrupted",
        }[target]
        self._event(
            job_id=job_id,
            event_type=event_name,
            stage=changed.stage,
            progress=changed.progress,
            summary=summary,
            at=at,
        )
        return changed

    def list_events(self, *, after_event_id: int, limit: int) -> list[JobEvent]:
        rows = self._session.scalars(
            select(JobEventRow)
            .where(JobEventRow.event_id > after_event_id)
            .order_by(JobEventRow.event_id)
            .limit(limit)
        )
        return [
            JobEvent(
                event_id=row.event_id,
                job_id=row.job_id,
                event_type=row.event_type,
                stage=row.stage,
                progress=row.progress,
                summary=row.summary,
                occurred_at=UtcTimestamp.from_iso(row.occurred_at),
            )
            for row in rows
        ]

    def append_audit(
        self,
        *,
        actor: str,
        entity: str,
        action: str,
        correlation_id: str,
        occurred_at: UtcTimestamp,
        details: Mapping[str, str],
    ) -> AuditRecord:
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
        return AuditRecord(
            audit_id=row.audit_id,
            actor=row.actor,
            entity=row.entity,
            action=row.action,
            correlation_id=row.correlation_id,
            occurred_at=occurred_at,
            details=details,
        )

    def list_audits(self, *, action: str | None, limit: int) -> list[AuditRecord]:
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

    def recover_stale_running(
        self,
        *,
        stale_before: UtcTimestamp,
        at: UtcTimestamp,
        reason: str,
    ) -> tuple[str, ...]:
        rows = list(
            self._session.scalars(
                select(JobRunRow).where(
                    JobRunRow.status == JobStatus.RUNNING.value,
                    JobRunRow.heartbeat_at < stale_before.to_iso(),
                )
            )
        )
        recovered: list[str] = []
        for row in rows:
            failure = FailureInfo(
                error_type="WorkerInterrupted",
                message=reason,
                retryable=True,
            )
            changed = transition_job(
                self._domain(row), JobStatus.INTERRUPTED, at=at, failure=failure
            )
            outcome = self._session.execute(
                update(JobRunRow)
                .where(
                    JobRunRow.job_id == row.job_id,
                    JobRunRow.status == JobStatus.RUNNING.value,
                    JobRunRow.row_version == row.row_version,
                    JobRunRow.heartbeat_at == row.heartbeat_at,
                    JobRunRow.heartbeat_at < stale_before.to_iso(),
                )
                .values(**_row_values(changed))
            )
            if getattr(outcome, "rowcount", 0) != 1:
                continue
            recovered.append(row.job_id)
            self._event(
                job_id=row.job_id,
                event_type="job_interrupted",
                stage=row.stage,
                progress=row.progress,
                summary=reason,
                at=at,
            )
        return tuple(recovered)

    def heartbeat(self, job_id: str, *, at: UtcTimestamp, minimum_interval_ms: int) -> bool:
        row = self._row(job_id)
        if row.status != JobStatus.RUNNING.value:
            return False
        previous = _timestamp(row.heartbeat_at)
        if previous is not None:
            elapsed_ms = (at.value - previous.value).total_seconds() * 1000
            if elapsed_ms < minimum_interval_ms:
                return False
        updated_job_id = self._session.scalar(
            update(JobRunRow)
            .where(
                JobRunRow.job_id == job_id,
                JobRunRow.status == JobStatus.RUNNING.value,
                JobRunRow.row_version == row.row_version,
                JobRunRow.heartbeat_at == row.heartbeat_at,
            )
            .values(
                heartbeat_at=at.to_iso(),
                row_version=row.row_version + 1,
            )
            .returning(JobRunRow.job_id)
        )
        if updated_job_id is None:
            return False
        self._event(
            job_id=job_id,
            event_type="job_heartbeat",
            stage=row.stage,
            progress=row.progress,
            summary="worker heartbeat",
            at=at,
        )
        return True

    def report_progress(
        self,
        job_id: str,
        *,
        stage: str,
        progress: float,
        summary: str,
        at: UtcTimestamp,
        minimum_interval_ms: int,
    ) -> bool:
        if not 0.0 <= progress <= 1.0:
            raise ValueError("progress must be between 0 and 1")
        row = self._row(job_id)
        if row.status != JobStatus.RUNNING.value:
            return False
        previous = self._session.scalar(
            select(JobEventRow)
            .where(
                JobEventRow.job_id == job_id,
                JobEventRow.event_type == "job_progress",
            )
            .order_by(JobEventRow.event_id.desc())
            .limit(1)
        )
        if (
            previous is not None
            and previous.stage == stage
            and previous.progress == progress
            and previous.summary == summary
            and (at.value - UtcTimestamp.from_iso(previous.occurred_at).value).total_seconds()
            * 1000
            < minimum_interval_ms
        ):
            return False
        row.stage = stage
        row.progress = progress
        row.row_version += 1
        self._event(
            job_id=job_id,
            event_type="job_progress",
            stage=stage,
            progress=progress,
            summary=summary,
            at=at,
        )
        return True

    def request_cancel(
        self,
        job_id: str,
        *,
        reason: str,
        at: UtcTimestamp,
        expected_row_version: int | None,
        reject_if_non_cancellable: bool,
    ) -> JobRun:
        row = self._row(job_id)
        domain = self._domain(row)
        if expected_row_version is not None and domain.row_version != expected_row_version:
            raise JobConflictError(
                "job row_version changed before cancellation",
                context={"job_id": job_id},
            )
        if (
            reject_if_non_cancellable
            and domain.status is JobStatus.RUNNING
            and domain.non_cancellable_phase is not None
        ):
            raise JobConflictError(
                "job is in a non-cancellable critical section",
                context={"job_id": job_id},
            )
        if domain.status is JobStatus.PENDING:
            changed = transition_job(
                domain,
                JobStatus.CANCELLED,
                at=at,
                cancel_result="cancelled before claim",
            )
            changed = replace(
                changed,
                cancel_requested_at=at,
                cancel_request=reason,
            )
            self._apply(row, changed)
            self._event(
                job_id=job_id,
                event_type="job_cancelled",
                stage=row.stage,
                progress=row.progress,
                summary=reason,
                at=at,
            )
            return changed
        if domain.status is not JobStatus.RUNNING:
            return domain
        row.cancel_requested_at = at.to_iso()
        row.cancel_request = reason
        row.row_version += 1
        self._event(
            job_id=job_id,
            event_type="cancel_requested",
            stage=row.stage,
            progress=row.progress,
            summary=reason,
            at=at,
        )
        return self._domain(row)

    def add_artifact(self, job_id: str, *, relative_path: str, at: UtcTimestamp) -> JobRun:
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or "\\" in relative_path
            or relative_path.startswith("/")
            or ".." in relative_path.split("/")
        ):
            raise ValueError("artifact path must be a safe managed relative path")
        row = self._row(job_id)
        domain = self._domain(row)
        if domain.status is not JobStatus.RUNNING:
            raise ValueError("artifacts may only be attached to a RUNNING job")
        if relative_path in domain.artifacts:
            return domain
        row.artifacts_json = _json_sequence((*domain.artifacts, relative_path))
        row.row_version += 1
        self._event(
            job_id=job_id,
            event_type="job_artifact",
            stage=row.stage,
            progress=row.progress,
            summary="managed artifact recorded",
            at=at,
        )
        return self._domain(row)

    def add_recovery_artifact(
        self,
        job_id: str,
        *,
        relative_path: str,
        at: UtcTimestamp,
    ) -> JobRun:
        row = self._row(job_id)
        domain = self._domain(row)
        if domain.status not in {JobStatus.FAILED, JobStatus.INTERRUPTED}:
            raise JobConflictError("recovery artifacts require a failed or interrupted job")
        if (
            not relative_path.startswith("artifacts/eod/")
            or not relative_path.endswith(".incomplete")
            or "\\" in relative_path
            or ".." in relative_path.split("/")
        ):
            raise ValueError("recovery artifact path is invalid")
        if relative_path in domain.artifacts:
            return domain
        row.artifacts_json = _json_sequence((*domain.artifacts, relative_path))
        row.row_version += 1
        self._event(
            job_id=job_id,
            event_type="job_recovery_artifact",
            stage=row.stage,
            progress=row.progress,
            summary="interrupted EOD failure evidence attached",
            at=at,
        )
        return self._domain(row)

    def upstream_jobs_succeeded(self, job_id: str) -> bool:
        total = self._session.scalar(
            select(func.count())
            .select_from(JobDependencyRow)
            .where(JobDependencyRow.job_id == job_id)
        )
        if not total:
            return True
        succeeded = self._session.scalar(
            select(func.count())
            .select_from(JobDependencyRow)
            .join(JobRunRow, JobRunRow.job_id == JobDependencyRow.upstream_job_id)
            .where(
                JobDependencyRow.job_id == job_id,
                JobRunRow.status == JobStatus.SUCCEEDED.value,
            )
        )
        return succeeded == total

    def set_non_cancellable_phase(
        self,
        job_id: str,
        *,
        phase: str | None,
        at: UtcTimestamp,
    ) -> JobRun:
        row = self._row(job_id)
        if row.status != JobStatus.RUNNING.value:
            raise ValueError("non-cancellable phase requires a RUNNING job")
        row.non_cancellable_phase = phase
        row.row_version += 1
        self._event(
            job_id=job_id,
            event_type="non_cancellable_phase",
            stage=row.stage,
            progress=row.progress,
            summary="phase cleared" if phase is None else f"phase entered: {phase}",
            at=at,
        )
        return self._domain(row)

    def record_activity(
        self,
        job_id: str,
        *,
        worker_id: str,
        stage: str,
        progress: float,
        summary: str,
        audit_action: str,
        at: UtcTimestamp,
    ) -> None:
        if not 0.0 <= progress <= 1.0:
            raise ValueError("progress must be between 0 and 1")
        row = self._row(job_id)
        if row.status != JobStatus.RUNNING.value:
            raise ValueError("activity requires a RUNNING job")
        self._session.execute(
            update(JobRunRow)
            .where(JobRunRow.job_id == job_id, JobRunRow.status == JobStatus.RUNNING.value)
            .values(
                heartbeat_at=at.to_iso(),
                stage=stage,
                progress=progress,
                row_version=JobRunRow.row_version + 1,
            )
        )
        self._event(
            job_id=job_id,
            event_type="job_activity",
            stage=stage,
            progress=progress,
            summary=summary,
            at=at,
        )
        self.append_audit(
            actor=worker_id,
            entity=job_id,
            action=audit_action,
            correlation_id=job_id,
            occurred_at=at,
            details={"summary": summary},
        )


class SqliteUnitOfWork:
    """Fresh session per retry attempt; exceptions always roll back."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self.jobs: SQLiteJobRepository

    def __enter__(self) -> Self:
        self._session = self._session_factory()
        self.jobs = SQLiteJobRepository(self._session)
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


class SqliteUnitOfWorkFactory:
    """Callable unit-of-work factory safe to share within one process."""

    def __init__(self, engine: Engine) -> None:
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def __call__(self) -> SqliteUnitOfWork:
        return SqliteUnitOfWork(self._session_factory)

    @staticmethod
    def _is_lock_error(error: OperationalError) -> bool:
        original = error.orig
        if not isinstance(original, sqlite3.OperationalError):
            return False
        code = getattr(original, "sqlite_errorcode", None)
        if isinstance(code, int) and (code & 0xFF) in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        }:
            return True
        message = str(original).casefold()
        return message in {
            "database is locked",
            "database table is locked",
            "database schema is locked",
        }

    @staticmethod
    def _delay(policy: RetryPolicy, attempt: int) -> float:
        bounded_ms = min(policy.cap_delay_ms, policy.base_delay_ms * (2**attempt))
        jitter = float(random.uniform(-policy.jitter_ratio, policy.jitter_ratio))
        delay_ms: float = float(bounded_ms) * (1.0 + jitter)
        return max(0.0, delay_ms) / 1000.0

    def execute(
        self,
        operation: Callable[[JobRepository], ResultT],
        policy: RetryPolicy,
    ) -> ResultT:
        """Run a replay-safe repository operation in a fresh session per attempt."""
        last_error: OperationalError | None = None
        for attempt in range(policy.max_attempts):
            try:
                with self() as unit_of_work:
                    result = operation(unit_of_work.jobs)
                    unit_of_work.commit()
                    return result
            except OperationalError as error:
                if not self._is_lock_error(error):
                    raise
                last_error = error
                if attempt + 1 < policy.max_attempts:
                    time.sleep(self._delay(policy, attempt))
        raise PersistenceBusyError(
            "SQLite remained busy after bounded retries",
            context={"attempts": str(policy.max_attempts)},
        ) from last_error
