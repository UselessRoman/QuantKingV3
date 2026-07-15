"""Framework-free durable job, event, and audit domain objects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType

from quantking.common.errors import InvalidJobTransitionError
from quantking.common.ids import parse_entity_id
from quantking.common.time import UtcTimestamp


class JobStatus(StrEnum):
    """Persisted lifecycle states for a generic long-running job."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


TERMINAL_STATUSES = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    }
)
ALLOWED_TRANSITIONS: Mapping[JobStatus, frozenset[JobStatus]] = MappingProxyType(
    {
        JobStatus.PENDING: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
        JobStatus.RUNNING: TERMINAL_STATUSES,
        JobStatus.SUCCEEDED: frozenset(),
        JobStatus.FAILED: frozenset(),
        JobStatus.CANCELLED: frozenset(),
        JobStatus.INTERRUPTED: frozenset(),
    }
)


def _text(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


def _manifest(value: Mapping[str, str], *, field: str) -> Mapping[str, str]:
    normalized: dict[str, str] = {}
    for key, item in value.items():
        normalized[_text(key, field=f"{field} key")] = _text(item, field=f"{field} value")
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True, slots=True)
class FailureInfo:
    """Stable structured failure kept on a failed or interrupted job."""

    error_type: str
    message: str
    retryable: bool

    def __post_init__(self) -> None:
        _text(self.error_type, field="error_type")
        _text(self.message, field="message")
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be a bool")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded SQLite lock retry and event write-throttling parameters."""

    max_attempts: int = 7
    base_delay_ms: int = 10
    cap_delay_ms: int = 160
    jitter_ratio: float = 0.25
    heartbeat_min_interval_ms: int = 1000
    progress_min_interval_ms: int = 250

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        if (
            not 0 <= self.base_delay_ms <= 5000
            or not self.base_delay_ms <= self.cap_delay_ms <= 5000
        ):
            raise ValueError("retry delay bounds are invalid")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError("jitter_ratio must be between 0 and 1")
        if not 1 <= self.heartbeat_min_interval_ms <= 60_000:
            raise ValueError("heartbeat_min_interval_ms must be between 1 and 60000")
        if not 1 <= self.progress_min_interval_ms <= 60_000:
            raise ValueError("progress_min_interval_ms must be between 1 and 60000")


@dataclass(frozen=True, slots=True)
class JobRun:
    """Domain representation, deliberately separate from SQLAlchemy rows."""

    job_id: str
    job_type: str
    idempotency_key: str
    input_manifest: Mapping[str, str]
    config_manifest: Mapping[str, str]
    version_manifest: Mapping[str, str]
    requester: str
    status: JobStatus
    created_at: UtcTimestamp
    started_at: UtcTimestamp | None
    ended_at: UtcTimestamp | None
    heartbeat_at: UtcTimestamp | None
    stage: str | None
    progress: float
    worker_id: str | None
    log_id: str
    artifacts: tuple[str, ...]
    failure: FailureInfo | None
    cancel_requested_at: UtcTimestamp | None
    cancel_request: str | None
    cancel_result: str | None
    non_cancellable_phase: str | None
    row_version: int
    upstream_job_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        parsed = parse_entity_id(self.job_id)
        if parsed.namespace != "job":
            raise ValueError("job_id must use the job namespace")
        for field, value in (
            ("job_type", self.job_type),
            ("idempotency_key", self.idempotency_key),
            ("requester", self.requester),
            ("log_id", self.log_id),
        ):
            _text(value, field=field)
        if not isinstance(self.status, JobStatus):
            raise TypeError("status must be JobStatus")
        if not 0.0 <= self.progress <= 1.0:
            raise ValueError("progress must be between 0 and 1")
        if self.row_version < 1:
            raise ValueError("row_version must be positive")
        object.__setattr__(
            self, "input_manifest", _manifest(self.input_manifest, field="input_manifest")
        )
        object.__setattr__(
            self, "config_manifest", _manifest(self.config_manifest, field="config_manifest")
        )
        object.__setattr__(
            self, "version_manifest", _manifest(self.version_manifest, field="version_manifest")
        )
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "upstream_job_ids", tuple(self.upstream_job_ids))

    @classmethod
    def pending(
        cls,
        *,
        job_id: str,
        job_type: str,
        idempotency_key: str,
        input_manifest: Mapping[str, str],
        config_manifest: Mapping[str, str],
        version_manifest: Mapping[str, str],
        requester: str,
        created_at: UtcTimestamp,
        log_id: str,
        progress: float = 0.0,
        upstream_job_ids: tuple[str, ...] = (),
    ) -> JobRun:
        """Create a validated pending job without persistence side effects."""
        return cls(
            job_id=job_id,
            job_type=job_type,
            idempotency_key=idempotency_key,
            input_manifest=input_manifest,
            config_manifest=config_manifest,
            version_manifest=version_manifest,
            requester=requester,
            status=JobStatus.PENDING,
            created_at=created_at,
            started_at=None,
            ended_at=None,
            heartbeat_at=None,
            stage=None,
            progress=progress,
            worker_id=None,
            log_id=log_id,
            artifacts=(),
            failure=None,
            cancel_requested_at=None,
            cancel_request=None,
            cancel_result=None,
            non_cancellable_phase=None,
            row_version=1,
            upstream_job_ids=upstream_job_ids,
        )


@dataclass(frozen=True, slots=True)
class JobEvent:
    """Low-frequency persisted event read through a monotonic cursor."""

    event_id: int
    job_id: str
    event_type: str
    stage: str | None
    progress: float | None
    summary: str
    occurred_at: UtcTimestamp


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """Append-only generic audit fact."""

    audit_id: int
    actor: str
    entity: str
    action: str
    correlation_id: str
    occurred_at: UtcTimestamp
    details: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", _manifest(self.details, field="details"))


def transition_job(
    job: JobRun,
    target: JobStatus,
    *,
    at: UtcTimestamp,
    worker_id: str | None = None,
    failure: FailureInfo | None = None,
    cancel_result: str | None = None,
) -> JobRun:
    """Apply the single authoritative job transition table."""
    if target not in ALLOWED_TRANSITIONS[job.status]:
        raise InvalidJobTransitionError(
            "job status transition is not allowed",
            context={"current": job.status.value, "target": target.value},
        )
    if target is JobStatus.RUNNING and not worker_id:
        raise InvalidJobTransitionError("RUNNING requires a worker_id")
    if target in {JobStatus.FAILED, JobStatus.INTERRUPTED} and failure is None:
        raise InvalidJobTransitionError(f"{target.value} requires structured failure")

    terminal = target in TERMINAL_STATUSES
    return replace(
        job,
        status=target,
        started_at=at if target is JobStatus.RUNNING else job.started_at,
        ended_at=at if terminal else None,
        heartbeat_at=at if target is JobStatus.RUNNING else job.heartbeat_at,
        worker_id=worker_id if target is JobStatus.RUNNING else job.worker_id,
        progress=1.0 if target is JobStatus.SUCCEEDED else job.progress,
        failure=failure,
        cancel_result=cancel_result,
        row_version=job.row_version + 1,
    )
