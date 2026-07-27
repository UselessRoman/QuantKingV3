"""Explicit local Worker registry and transaction-free handler runner."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from quantking.common.errors import UnknownJobTypeError
from quantking.jobs.models import FailureInfo, JobStatus
from quantking.jobs.services import JobService


class JobCancelledAtCheckpoint(Exception):
    """Cooperative cancellation raised only at a handler-declared safe point."""


@dataclass(frozen=True, slots=True)
class WorkerContext:
    """Minimal context exposed to a registered generic handler."""

    service: JobService
    job_id: str

    def checkpoint(self) -> None:
        """Stop only when cancellation is requested and the phase is cancellable."""
        job = self.service.get_job(self.job_id)
        if job.cancel_requested_at is not None and job.non_cancellable_phase is None:
            raise JobCancelledAtCheckpoint(job.cancel_request or "cancellation requested")

    def heartbeat(self) -> bool:
        return self.service.heartbeat(self.job_id)

    def progress(self, *, stage: str, progress: float, summary: str) -> bool:
        return self.service.report_progress(
            self.job_id,
            stage=stage,
            progress=progress,
            summary=summary,
        )

    def activity(
        self,
        *,
        stage: str,
        progress: float,
        summary: str,
        audit_action: str,
    ) -> None:
        """Persist one low-frequency event and audit fact atomically."""
        job = self.service.get_job(self.job_id)
        if job.worker_id is None:
            raise ValueError("worker activity requires a claimed job")
        self.service.record_activity(
            self.job_id,
            worker_id=job.worker_id,
            stage=stage,
            progress=progress,
            summary=summary,
            audit_action=audit_action,
        )

    @contextmanager
    def non_cancellable(self, phase: str) -> Iterator[None]:
        """Persist a short non-cancellable phase and always clear it afterward."""
        self.service.set_non_cancellable_phase(self.job_id, phase=phase)
        try:
            yield
        finally:
            self.service.set_non_cancellable_phase(self.job_id, phase=None)


JobHandler = Callable[[WorkerContext], object]


class LeaseRenewer(Protocol):
    """Maintain a persisted Worker lease for the complete handler lifecycle."""

    def maintain(self, heartbeat: Callable[[], bool]) -> AbstractContextManager[None]: ...


class ThreadLeaseRenewer:
    """Low-frequency daemon renewer; interval must remain below the recovery lease."""

    def __init__(self, *, interval_seconds: float = 30.0) -> None:
        if not isinstance(interval_seconds, (int, float)) or not 0 < interval_seconds < 120:
            raise ValueError("heartbeat interval must be positive and below the Worker lease")
        self._interval = float(interval_seconds)

    @contextmanager
    def maintain(self, heartbeat: Callable[[], bool]) -> Iterator[None]:
        stop = threading.Event()
        failure: list[BaseException] = []

        def renew() -> None:
            while not stop.wait(self._interval):
                try:
                    heartbeat()
                except BaseException as error:
                    failure.append(error)
                    stop.set()

        thread = threading.Thread(target=renew, name="quantking-worker-lease", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=self._interval + 1.0)
        if failure:
            raise RuntimeError("Worker heartbeat lease renewal failed") from failure[0]


class WorkerRegistry:
    """Explicit job-type-to-handler map; discovery and schedulers are out of scope."""

    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}

    def register(self, job_type: str, handler: JobHandler) -> None:
        if not isinstance(job_type, str) or not job_type.strip():
            raise ValueError("job_type must be non-empty text")
        if job_type in self._handlers:
            raise ValueError(f"handler already registered for {job_type}")
        self._handlers[job_type] = handler

    def resolve(self, job_type: str) -> JobHandler:
        try:
            return self._handlers[job_type]
        except KeyError as error:
            raise UnknownJobTypeError(
                "no handler is registered for job type",
                context={"job_type": job_type},
            ) from error


class Worker:
    """Claim atomically, run the handler outside transactions, then persist outcome."""

    def __init__(
        self,
        service: JobService,
        registry: WorkerRegistry,
        *,
        worker_id: str,
        lease_renewer: LeaseRenewer | None = None,
    ) -> None:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be non-empty text")
        self._service = service
        self._registry = registry
        self._worker_id = worker_id
        self._lease_renewer = lease_renewer or ThreadLeaseRenewer()

    def run(self, job_id: str) -> None:
        job = self._service.get_job(job_id)
        handler = self._registry.resolve(job.job_type)
        self._service.require_upstreams_succeeded(job_id)
        self._service.transition(
            job_id,
            JobStatus.RUNNING,
            worker_id=self._worker_id,
            summary="worker claimed job",
        )
        context = WorkerContext(self._service, job_id)
        with self._lease_renewer.maintain(context.heartbeat):
            try:
                result = handler(context)
                if result is not None:
                    raise TypeError("registered job handlers must return None")
            except JobCancelledAtCheckpoint:
                self._service.transition(
                    job_id,
                    JobStatus.CANCELLED,
                    cancel_result="cancelled at safe checkpoint",
                    summary="job cancelled at safe checkpoint",
                )
                raise
            except BaseException as error:
                self._service.transition(
                    job_id,
                    JobStatus.FAILED,
                    failure=FailureInfo(
                        error_type=type(error).__name__,
                        message="registered job handler failed; inspect the managed JSONL log",
                        retryable=False,
                    ),
                    summary="job handler failed",
                )
                raise
            try:
                self._service.transition(
                    job_id,
                    JobStatus.SUCCEEDED,
                    summary="job handler succeeded",
                )
            except BaseException as error:
                observer = getattr(handler, "on_success_commit_failure", None)
                if callable(observer):
                    observer(context, error)
                raise
