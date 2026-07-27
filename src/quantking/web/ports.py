"""Read-only application ports consumed by the local Web transport."""

from __future__ import annotations

from typing import Protocol

from quantking.data.eod.contracts import EodRunRequest
from quantking.data.eod.service import EodEnqueueResult
from quantking.jobs.models import JobEvent, JobRun


class JobReadPort(Protocol):
    """The only durable-job capabilities exposed to Web routes."""

    def get_job(self, job_id: str) -> JobRun: ...

    def list_events(self, *, after_event_id: int, limit: int) -> list[JobEvent]: ...


class EventReadPort(Protocol):
    """Narrow event-only capability used by the SSE transport."""

    def list_events(self, *, after_event_id: int, limit: int) -> list[JobEvent]: ...


class EodCommandPort(Protocol):
    """Narrow EOD enqueue capability exposed to Web and CLI transports."""

    def enqueue(
        self,
        request: EodRunRequest,
        *,
        idempotency_key: str,
        requester: str,
    ) -> EodEnqueueResult: ...


class JobCommandPort(Protocol):
    """Optimistic durable-job commands exposed to Web."""

    def request_cancel(
        self,
        job_id: str,
        *,
        reason: str,
        expected_row_version: int | None = None,
        reject_if_non_cancellable: bool = False,
    ) -> JobRun: ...
