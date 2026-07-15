"""Application service for replay-safe, short durable job transactions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from quantking.common.errors import UpstreamJobNotReadyError
from quantking.common.ids import new_entity_id
from quantking.common.time import Clock, UtcTimestamp, utc_now
from quantking.jobs.models import (
    AuditRecord,
    FailureInfo,
    JobEvent,
    JobRun,
    JobStatus,
    RetryPolicy,
)
from quantking.jobs.ports import JobRepository, TransactionManager

ResultT = TypeVar("ResultT")


def _required_text(value: str, *, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")


@dataclass(frozen=True, slots=True)
class CreateJob:
    """Explicit input for an idempotent generic job creation command."""

    job_type: str
    idempotency_key: str
    input_manifest: Mapping[str, str]
    config_manifest: Mapping[str, str]
    version_manifest: Mapping[str, str]
    requester: str
    log_id: str
    upstream_job_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field, value in (
            ("job_type", self.job_type),
            ("idempotency_key", self.idempotency_key),
            ("requester", self.requester),
            ("log_id", self.log_id),
        ):
            _required_text(value, field=field)


class JobService:
    """Keep handlers and long calculation outside all repository transactions."""

    def __init__(
        self,
        transactions: TransactionManager,
        *,
        retry_policy: RetryPolicy | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._transactions = transactions
        self._retry_policy = retry_policy or RetryPolicy()
        self._clock = clock

    def _now(self) -> UtcTimestamp:
        return utc_now(clock=self._clock)

    def _execute(self, operation: Callable[[JobRepository], ResultT]) -> ResultT:
        return self._transactions.execute(operation, self._retry_policy)

    def create_job(self, command: CreateJob) -> JobRun:
        created_at = self._now()
        candidate = JobRun.pending(
            job_id=str(new_entity_id("job")),
            job_type=command.job_type,
            idempotency_key=command.idempotency_key,
            input_manifest=command.input_manifest,
            config_manifest=command.config_manifest,
            version_manifest=command.version_manifest,
            requester=command.requester,
            created_at=created_at,
            log_id=command.log_id,
            upstream_job_ids=command.upstream_job_ids,
        )
        return self._execute(lambda repository: repository.create_or_get(candidate)[0])

    def get_job(self, job_id: str) -> JobRun:
        return self._execute(lambda repository: repository.get(job_id))

    def transition(
        self,
        job_id: str,
        target: JobStatus,
        *,
        worker_id: str | None = None,
        failure: FailureInfo | None = None,
        cancel_result: str | None = None,
        summary: str | None = None,
        at: UtcTimestamp | None = None,
    ) -> JobRun:
        occurred_at = at or self._now()
        event_summary = summary or f"job transitioned to {target.value}"
        return self._execute(
            lambda repository: repository.transition(
                job_id,
                target,
                at=occurred_at,
                worker_id=worker_id,
                failure=failure,
                cancel_result=cancel_result,
                summary=event_summary,
            )
        )

    def list_events(self, *, after_event_id: int, limit: int) -> list[JobEvent]:
        if (
            isinstance(after_event_id, bool)
            or not isinstance(after_event_id, int)
            or after_event_id < 0
        ):
            raise ValueError("after_event_id must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        return self._execute(
            lambda repository: repository.list_events(
                after_event_id=after_event_id,
                limit=limit,
            )
        )

    def append_audit(
        self,
        *,
        actor: str,
        entity: str,
        action: str,
        correlation_id: str,
        details: Mapping[str, str],
        at: UtcTimestamp | None = None,
    ) -> AuditRecord:
        for field, value in (
            ("actor", actor),
            ("entity", entity),
            ("action", action),
            ("correlation_id", correlation_id),
        ):
            _required_text(value, field=field)
        occurred_at = at or self._now()
        return self._execute(
            lambda repository: repository.append_audit(
                actor=actor,
                entity=entity,
                action=action,
                correlation_id=correlation_id,
                occurred_at=occurred_at,
                details=details,
            )
        )

    def list_audits(self, *, action: str | None = None, limit: int = 100) -> list[AuditRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        return self._execute(lambda repository: repository.list_audits(action=action, limit=limit))

    def recover_running_jobs(self, *, reason: str, at: UtcTimestamp | None = None) -> int:
        _required_text(reason, field="reason")
        occurred_at = at or self._now()
        return self._execute(
            lambda repository: repository.recover_running(at=occurred_at, reason=reason)
        )

    def heartbeat(self, job_id: str, *, at: UtcTimestamp | None = None) -> bool:
        occurred_at = at or self._now()
        return self._execute(
            lambda repository: repository.heartbeat(
                job_id,
                at=occurred_at,
                minimum_interval_ms=self._retry_policy.heartbeat_min_interval_ms,
            )
        )

    def report_progress(
        self,
        job_id: str,
        *,
        stage: str,
        progress: float,
        summary: str,
        at: UtcTimestamp | None = None,
    ) -> bool:
        _required_text(stage, field="stage")
        _required_text(summary, field="summary")
        occurred_at = at or self._now()
        return self._execute(
            lambda repository: repository.report_progress(
                job_id,
                stage=stage,
                progress=progress,
                summary=summary,
                at=occurred_at,
                minimum_interval_ms=self._retry_policy.progress_min_interval_ms,
            )
        )

    def request_cancel(
        self,
        job_id: str,
        *,
        reason: str,
        at: UtcTimestamp | None = None,
    ) -> JobRun:
        _required_text(reason, field="reason")
        occurred_at = at or self._now()
        return self._execute(
            lambda repository: repository.request_cancel(job_id, reason=reason, at=occurred_at)
        )

    def require_upstreams_succeeded(self, job_id: str) -> None:
        ready = self._execute(lambda repository: repository.upstream_jobs_succeeded(job_id))
        if not ready:
            raise UpstreamJobNotReadyError(
                "all upstream jobs must succeed before claim",
                context={"job_id": job_id},
            )

    def set_non_cancellable_phase(
        self,
        job_id: str,
        *,
        phase: str | None,
        at: UtcTimestamp | None = None,
    ) -> JobRun:
        if phase is not None:
            _required_text(phase, field="phase")
        occurred_at = at or self._now()
        return self._execute(
            lambda repository: repository.set_non_cancellable_phase(
                job_id,
                phase=phase,
                at=occurred_at,
            )
        )

    def record_activity(
        self,
        job_id: str,
        *,
        worker_id: str,
        stage: str,
        progress: float,
        summary: str,
        audit_action: str,
        at: UtcTimestamp | None = None,
    ) -> None:
        occurred_at = at or self._now()
        self._execute(
            lambda repository: repository.record_activity(
                job_id,
                worker_id=worker_id,
                stage=stage,
                progress=progress,
                summary=summary,
                audit_action=audit_action,
                at=occurred_at,
            )
        )
