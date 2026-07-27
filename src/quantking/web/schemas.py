"""Stable public Web schemas and framework-free domain mappings."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from quantking.common.time import UtcTimestamp
from quantking.data.eod.contracts import EodPublishIntent
from quantking.jobs.models import FailureInfo, JobEvent, JobRun


class PublicSchema(BaseModel):
    """Reject accidental widening of a public response contract."""

    model_config = ConfigDict(extra="forbid")


class HealthResponse(PublicSchema):
    status: str
    service: str
    checked_at: str


class FailureResponse(PublicSchema):
    error_type: str
    message: str
    retryable: bool


class JobResponse(PublicSchema):
    job_id: str
    job_type: str
    status: str
    created_at: str
    started_at: str | None
    ended_at: str | None
    heartbeat_at: str | None
    stage: str | None
    progress: float
    worker_id: str | None
    failure: FailureResponse | None
    row_version: int


class JobEventResponse(PublicSchema):
    event_id: int
    job_id: str
    event_type: str
    stage: str | None
    progress: float | None
    summary: str
    occurred_at: str


class JobEventPage(PublicSchema):
    events: list[JobEventResponse]
    next_cursor: int


class ErrorDetail(PublicSchema):
    code: str
    message: str


class ErrorResponse(PublicSchema):
    error: ErrorDetail


class EodRunBody(PublicSchema):
    start_date: date
    end_date: date
    datasets: tuple[str, ...]
    publish_intent: EodPublishIntent
    base_versions: dict[str, str]


class EodEnqueueResponse(PublicSchema):
    job_id: str
    status: str
    row_version: int
    created: bool


class CancelJobBody(PublicSchema):
    expected_row_version: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=1, max_length=256)


def _timestamp(value: UtcTimestamp | None) -> str | None:
    if value is None:
        return None
    return value.to_iso()


def _map_failure(failure: FailureInfo | None) -> FailureResponse | None:
    if failure is None:
        return None
    return FailureResponse(
        error_type=failure.error_type,
        message=failure.message,
        retryable=failure.retryable,
    )


def map_job(job: JobRun) -> JobResponse:
    """Map a domain job without exposing manifests, paths, or mutable internals."""
    return JobResponse(
        job_id=job.job_id,
        job_type=job.job_type,
        status=job.status.value,
        created_at=job.created_at.to_iso(),
        started_at=_timestamp(job.started_at),
        ended_at=_timestamp(job.ended_at),
        heartbeat_at=_timestamp(job.heartbeat_at),
        stage=job.stage,
        progress=job.progress,
        worker_id=job.worker_id,
        failure=_map_failure(job.failure),
        row_version=job.row_version,
    )


def map_job_event(event: JobEvent) -> JobEventResponse:
    """Use one event mapping for REST and SSE."""
    return JobEventResponse(
        event_id=event.event_id,
        job_id=event.job_id,
        event_type=event.event_type,
        stage=event.stage,
        progress=event.progress,
        summary=event.summary,
        occurred_at=event.occurred_at.to_iso(),
    )
