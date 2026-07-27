"""FastAPI routes over injected, read-only application ports."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from quantking.common.time import Clock
from quantking.data.eod.contracts import EodRunRequest
from quantking.web.ports import EodCommandPort, JobCommandPort, JobReadPort
from quantking.web.schemas import (
    CancelJobBody,
    EodEnqueueResponse,
    EodRunBody,
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


def create_api_router(
    job_port: JobReadPort,
    *,
    eod_command_port: EodCommandPort | None = None,
    job_command_port: JobCommandPort | None = None,
    clock: Clock | None = None,
) -> APIRouter:
    """Create routes without opening a database or resolving a filesystem path."""
    router = APIRouter(prefix="/api")

    @router.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", service="quantking-web", checked_at=_now(clock))

    @router.get("/jobs/{job_id}", response_model=JobResponse)
    def get_job(job_id: str) -> JobResponse:
        return map_job(job_port.get_job(job_id))

    if eod_command_port is not None:

        @router.post(
            "/data/eod-runs",
            response_model=EodEnqueueResponse,
            status_code=202,
        )
        def enqueue_eod(
            body: EodRunBody,
            response: Response,
            idempotency_key: str = Header(alias="Idempotency-Key"),
        ) -> EodEnqueueResponse:
            outcome = eod_command_port.enqueue(
                EodRunRequest(
                    start_date=body.start_date,
                    end_date=body.end_date,
                    datasets=body.datasets,
                    publish_intent=body.publish_intent,
                ),
                idempotency_key=idempotency_key,
                requester="web-api",
            )
            response.headers["Location"] = f"/api/jobs/{outcome.job_id}"
            return EodEnqueueResponse(
                job_id=outcome.job_id,
                status=outcome.status.value,
                row_version=outcome.row_version,
                created=outcome.created,
            )

    if job_command_port is not None:

        @router.post("/jobs/{job_id}/cancel", response_model=JobResponse)
        def cancel_job(
            job_id: str,
            body: CancelJobBody,
            if_match: str | None = Header(default=None, alias="If-Match"),
        ) -> JobResponse:
            header_version: int | None = None
            if if_match is not None:
                normalized = if_match.strip().strip('"')
                try:
                    header_version = int(normalized)
                except ValueError as error:
                    raise InvalidRequestError("If-Match is invalid") from error
                if header_version < 1:
                    raise InvalidRequestError("If-Match is invalid")
            if body.expected_row_version is None and header_version is None:
                raise InvalidRequestError("expected row_version is required")
            if (
                body.expected_row_version is not None
                and header_version is not None
                and body.expected_row_version != header_version
            ):
                raise InvalidRequestError("row_version credentials conflict")
            expected = (
                body.expected_row_version
                if body.expected_row_version is not None
                else header_version
            )
            return map_job(
                job_command_port.request_cancel(
                    job_id,
                    reason=body.reason,
                    expected_row_version=expected,
                    reject_if_non_cancellable=True,
                )
            )

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
