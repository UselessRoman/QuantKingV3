"""Ports owned by the durable jobs application boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, Self, TypeVar

from quantking.common.time import UtcTimestamp
from quantking.jobs.models import AuditRecord, FailureInfo, JobEvent, JobRun, JobStatus, RetryPolicy

ResultT = TypeVar("ResultT")


class JobRepository(Protocol):
    """Short-transaction persistence operations used by the jobs service."""

    def create_or_get(self, job: JobRun) -> tuple[JobRun, bool]: ...

    def get(self, job_id: str) -> JobRun: ...

    def list_page(
        self,
        *,
        job_type: str | None,
        status: JobStatus | None,
        before: tuple[str, str] | None,
        limit: int,
    ) -> list[JobRun]: ...

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
    ) -> JobRun: ...

    def list_events(self, *, after_event_id: int, limit: int) -> list[JobEvent]: ...

    def append_audit(
        self,
        *,
        actor: str,
        entity: str,
        action: str,
        correlation_id: str,
        occurred_at: UtcTimestamp,
        details: Mapping[str, str],
    ) -> AuditRecord: ...

    def list_audits(self, *, action: str | None, limit: int) -> list[AuditRecord]: ...

    def recover_stale_running(
        self,
        *,
        stale_before: UtcTimestamp,
        at: UtcTimestamp,
        reason: str,
    ) -> tuple[str, ...]: ...

    def heartbeat(self, job_id: str, *, at: UtcTimestamp, minimum_interval_ms: int) -> bool: ...

    def report_progress(
        self,
        job_id: str,
        *,
        stage: str,
        progress: float,
        summary: str,
        at: UtcTimestamp,
        minimum_interval_ms: int,
    ) -> bool: ...

    def request_cancel(
        self,
        job_id: str,
        *,
        reason: str,
        at: UtcTimestamp,
        expected_row_version: int | None,
        reject_if_non_cancellable: bool,
    ) -> JobRun: ...

    def add_artifact(self, job_id: str, *, relative_path: str, at: UtcTimestamp) -> JobRun: ...

    def add_recovery_artifact(
        self,
        job_id: str,
        *,
        relative_path: str,
        at: UtcTimestamp,
    ) -> JobRun: ...

    def set_non_cancellable_phase(
        self,
        job_id: str,
        *,
        phase: str | None,
        at: UtcTimestamp,
    ) -> JobRun: ...

    def upstream_jobs_succeeded(self, job_id: str) -> bool: ...

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
    ) -> None: ...


class UnitOfWork(Protocol):
    """A fresh SQLAlchemy session and repository for one short transaction."""

    jobs: JobRepository

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]


class TransactionManager(Protocol):
    """Adapter-owned retry boundary, keeping SQLAlchemy out of application services."""

    def execute(
        self,
        operation: Callable[[JobRepository], ResultT],
        policy: RetryPolicy,
    ) -> ResultT: ...
