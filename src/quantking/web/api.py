"""FastAPI routes over injected, read-only application ports."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from quantking.common.time import Clock
from quantking.data.center import DataCenterPage, validate_cursor
from quantking.data.eod.contracts import EodRunRequest
from quantking.data.models import (
    DatasetKind,
    DatasetVersionStatus,
    DataUpstream,
    IngestionBatchStatus,
    QualityRunStatus,
)
from quantking.jobs.models import JobStatus
from quantking.web.data_center import DataCenterReadPort
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


class InvalidEodRequestError(Exception):
    """Stable EOD command validation failure without internal input echo."""


def _now(clock: Clock | None) -> str:
    value = clock() if clock is not None else datetime.now(UTC)
    return value.astimezone(UTC).isoformat().removesuffix("+00:00") + "Z"


def create_api_router(
    job_port: JobReadPort,
    *,
    eod_command_port: EodCommandPort | None = None,
    job_command_port: JobCommandPort | None = None,
    clock: Clock | None = None,
    data_center_port: DataCenterReadPort | None = None,
) -> APIRouter:
    """Create routes without opening a database or resolving a filesystem path."""
    router = APIRouter(prefix="/api")

    @router.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", service="quantking-web", checked_at=_now(clock))

    @router.get("/jobs/{job_id}", response_model=JobResponse)
    def get_job(job_id: str) -> JobResponse:
        return map_job(job_port.get_job(job_id))

    if data_center_port is not None:

        def page(value: DataCenterPage) -> dict[str, object]:
            return {
                "items": list(value.items),
                "next_cursor": value.next_cursor,
            }

        @router.get("/data/summary")
        def data_summary() -> object:
            return data_center_port.summary()

        @router.get("/data/batches")
        def data_batches(
            dataset: DatasetKind | None = None,
            upstream: DataUpstream | None = None,
            status: IngestionBatchStatus | None = None,
            started_from: datetime | None = None,
            started_to: datetime | None = None,
            cursor: str | None = None,
            limit: int = Query(default=50, ge=1, le=100),
        ) -> object:
            validate_cursor(cursor, "batches")
            return page(
                data_center_port.list_batches(
                    dataset=dataset.value if dataset else None,
                    upstream=upstream.value if upstream else None,
                    status=status.value if status else None,
                    started_from=started_from.isoformat() if started_from else None,
                    started_to=started_to.isoformat() if started_to else None,
                    cursor=cursor,
                    limit=limit,
                )
            )

        @router.get("/data/versions")
        def data_versions(
            dataset: DatasetKind | None = None,
            status: DatasetVersionStatus | None = None,
            cursor: str | None = None,
            limit: int = Query(default=50, ge=1, le=100),
        ) -> object:
            validate_cursor(cursor, "versions")
            return page(
                data_center_port.list_versions(
                    dataset=dataset.value if dataset else None,
                    status=status.value if status else None,
                    cursor=cursor,
                    limit=limit,
                )
            )

        @router.get("/data/versions/{version_id}")
        def data_version(version_id: str) -> object:
            return data_center_port.get_version(version_id)

        @router.get("/data/versions/{version_id}/preview")
        def data_preview(
            version_id: str,
            fields: list[str] | None = None,
            codes: list[str] | None = None,
            start_date: date | None = None,
            end_date: date | None = None,
            cursor: str | None = None,
            limit: int = Query(default=50, ge=1, le=200),
        ) -> object:
            validate_cursor(cursor, f"preview:{version_id}")
            return page(
                data_center_port.preview(
                    version_id=version_id,
                    fields=fields,
                    codes=codes,
                    start_date=start_date.isoformat() if start_date else None,
                    end_date=end_date.isoformat() if end_date else None,
                    cursor=cursor,
                    limit=limit,
                )
            )

        @router.get("/data/quality-runs")
        def quality_runs(
            version_id: str | None = None,
            status: QualityRunStatus | None = None,
            cursor: str | None = None,
            limit: int = Query(default=50, ge=1, le=100),
        ) -> object:
            validate_cursor(cursor, "quality")
            return page(
                data_center_port.list_quality_runs(
                    version_id=version_id,
                    status=status.value if status else None,
                    cursor=cursor,
                    limit=limit,
                )
            )

        @router.get("/data/quality-runs/{run_id}")
        def quality_run(run_id: str) -> object:
            return data_center_port.get_quality_run(run_id)

        @router.get("/data/difference-reports")
        def difference_reports(
            version_id: str | None = None,
            primary_source: DataUpstream | None = None,
            cursor: str | None = None,
            limit: int = Query(default=50, ge=1, le=100),
        ) -> object:
            validate_cursor(cursor, "differences")
            return page(
                data_center_port.list_difference_reports(
                    version_id=version_id,
                    primary_source=primary_source.value if primary_source else None,
                    cursor=cursor,
                    limit=limit,
                )
            )

        @router.get("/data/difference-reports/{report_id}")
        def difference_report(report_id: str) -> object:
            return data_center_port.get_difference_report(report_id)

        @router.get("/data/difference-reports/{report_id}/rows")
        def difference_report_rows(
            report_id: str,
            cursor: str | None = None,
            limit: int = Query(default=50, ge=1, le=100),
        ) -> object:
            validate_cursor(cursor, f"difference-rows:{report_id}")
            return page(
                data_center_port.difference_rows(report_id=report_id, cursor=cursor, limit=limit)
            )

        @router.get("/data/resolution-runs")
        def resolution_runs(
            conflicts_only: bool = False,
            cursor: str | None = None,
            limit: int = Query(default=50, ge=1, le=100),
        ) -> object:
            validate_cursor(cursor, "resolutions")
            return page(
                data_center_port.list_resolution_runs(
                    conflicts_only=conflicts_only,
                    cursor=cursor,
                    limit=limit,
                )
            )

        @router.get("/data/resolution-runs/{run_id}")
        def resolution_run(run_id: str) -> object:
            return data_center_port.get_resolution_run(run_id)

        @router.get("/data/resolution-runs/{run_id}/exceptions")
        def resolution_exceptions(
            run_id: str,
            cursor: str | None = None,
            limit: int = Query(default=50, ge=1, le=100),
        ) -> object:
            validate_cursor(cursor, f"resolution-exceptions:{run_id}")
            return page(
                data_center_port.resolution_decisions(
                    run_id=run_id,
                    conflicts=False,
                    cursor=cursor,
                    limit=limit,
                )
            )

        @router.get("/data/resolution-runs/{run_id}/conflicts")
        def resolution_conflicts(
            run_id: str,
            cursor: str | None = None,
            limit: int = Query(default=50, ge=1, le=100),
        ) -> object:
            validate_cursor(cursor, f"resolution-conflicts:{run_id}")
            return page(
                data_center_port.resolution_decisions(
                    run_id=run_id,
                    conflicts=True,
                    cursor=cursor,
                    limit=limit,
                )
            )

        @router.get("/data/failures")
        def data_failures(
            cursor: str | None = None,
            limit: int = Query(default=50, ge=1, le=100),
        ) -> object:
            validate_cursor(cursor, "failures")
            return page(data_center_port.list_failures(cursor=cursor, limit=limit))

        @router.get("/data/eod-runs")
        def eod_runs(
            status: JobStatus | None = None,
            cursor: str | None = None,
            limit: int = Query(default=50, ge=1, le=100),
        ) -> object:
            validate_cursor(cursor, "eod")
            return page(
                data_center_port.list_eod_runs(
                    status=status.value if status else None,
                    cursor=cursor,
                    limit=limit,
                )
            )

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
            try:
                outcome = eod_command_port.enqueue(
                    EodRunRequest(
                        start_date=body.start_date,
                        end_date=body.end_date,
                        datasets=body.datasets,
                        publish_intent=body.publish_intent,
                        base_versions=body.base_versions,
                    ),
                    idempotency_key=idempotency_key,
                    requester="web-api",
                )
            except (TypeError, ValueError) as error:
                raise InvalidEodRequestError from error
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
