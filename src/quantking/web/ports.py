"""Read-only application ports consumed by the local Web transport."""

from __future__ import annotations

from typing import Protocol

from quantking.jobs.models import JobEvent, JobRun


class JobReadPort(Protocol):
    """The only durable-job capabilities exposed to Web routes."""

    def get_job(self, job_id: str) -> JobRun: ...

    def list_events(self, *, after_event_id: int, limit: int) -> list[JobEvent]: ...


class EventReadPort(Protocol):
    """Narrow event-only capability used by the SSE transport."""

    def list_events(self, *, after_event_id: int, limit: int) -> list[JobEvent]: ...
