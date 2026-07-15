"""FastAPI routes over injected, read-only application ports."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from quantking.common.time import Clock
from quantking.web.ports import JobReadPort
from quantking.web.schemas import (
    HealthResponse,
    JobEventPage,
    JobEventResponse,
    JobResponse,
    map_job,
    map_job_event,
)
from quantking.web.sse import event_stream, resolve_event_cursor


class InvalidRequestError(Exception):
    """Stable client-input failure raised after type validation."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _now(clock: Clock | None) -> str:
    value = clock() if clock is not None else datetime.now(UTC)
    return value.astimezone(UTC).isoformat().removesuffix("+00:00") + "Z"


def create_api_router(job_port: JobReadPort, *, clock: Clock | None = None) -> APIRouter:
    """Create routes without opening a database or resolving a filesystem path."""
    router = APIRouter(prefix="/api")

    @router.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", service="quantking-web", checked_at=_now(clock))

    @router.get("/jobs/{job_id}", response_model=JobResponse)
    def get_job(job_id: str) -> JobResponse:
        return map_job(job_port.get_job(job_id))

    @router.get("/job-events", response_model=JobEventPage)
    def get_events(
        after_event_id: int = Query(default=0),
        limit: int = Query(default=100),
    ) -> JobEventPage:
        if after_event_id < 0 or not 1 <= limit <= 1000:
            raise InvalidRequestError("event query bounds are invalid")
        mapped: list[JobEventResponse] = [
            map_job_event(event)
            for event in job_port.list_events(after_event_id=after_event_id, limit=limit)
        ]
        next_cursor = mapped[-1].event_id if mapped else after_event_id
        return JobEventPage(events=mapped, next_cursor=next_cursor)

    @router.get("/job-events/stream")
    def stream_events(
        request: Request,
        after_event_id: str | None = Query(default=None),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            cursor = resolve_event_cursor(
                last_event_id=last_event_id,
                after_event_id=after_event_id,
            )
        except ValueError as error:
            raise InvalidRequestError("event stream cursor is invalid") from error
        return StreamingResponse(
            event_stream(
                job_port,
                cursor=cursor,
                batch_size=100,
                poll_interval_seconds=0.5,
                is_disconnected=request.is_disconnected,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
