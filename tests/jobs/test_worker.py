from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from quantking.common.errors import UpstreamJobNotReadyError
from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    create_sqlite_engine,
    upgrade_database,
)
from quantking.jobs.models import JobStatus
from quantking.jobs.services import CreateJob, JobService
from quantking.jobs.worker import JobCancelledAtCheckpoint, Worker, WorkerContext, WorkerRegistry


def _service(tmp_path: Path) -> JobService:
    database = tmp_path / "worker.db"
    upgrade_database(database)
    return JobService(SqliteUnitOfWorkFactory(create_sqlite_engine(database, busy_timeout_ms=250)))


def _create(service: JobService, key: str, *, upstream: tuple[str, ...] = ()) -> str:
    return service.create_job(
        CreateJob(
            job_type="synthetic",
            idempotency_key=key,
            input_manifest={},
            config_manifest={},
            version_manifest={},
            requester="pytest",
            log_id=f"logs/{key}.jsonl",
            upstream_job_ids=upstream,
        )
    ).job_id


def test_worker_registry_is_explicit_and_handler_runs_outside_claim_transaction(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    job_id = _create(service, "success")
    observed: list[JobStatus] = []
    registry = WorkerRegistry()

    def handler(context: object) -> None:
        observed.append(service.get_job(job_id).status)

    registry.register("synthetic", handler)
    worker = Worker(service, registry, worker_id="worker-1")

    worker.run(job_id)

    assert observed == [JobStatus.RUNNING]
    assert service.get_job(job_id).status is JobStatus.SUCCEEDED


def test_worker_refuses_to_start_when_upstream_has_not_succeeded(tmp_path: Path) -> None:
    service = _service(tmp_path)
    upstream = _create(service, "upstream")
    downstream = _create(service, "downstream", upstream=(upstream,))
    registry = WorkerRegistry()
    registry.register("synthetic", lambda context: None)

    with pytest.raises(UpstreamJobNotReadyError):
        Worker(service, registry, worker_id="worker-1").run(downstream)

    assert service.get_job(downstream).status is JobStatus.PENDING


def test_worker_cancels_only_at_safe_checkpoint(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job_id = _create(service, "cancel")
    registry = WorkerRegistry()

    def handler(context: object) -> None:
        service.request_cancel(job_id, reason="pytest")
        context.checkpoint()  # type: ignore[attr-defined]
        raise AssertionError("checkpoint should stop the handler")

    registry.register("synthetic", handler)

    with pytest.raises(JobCancelledAtCheckpoint):
        Worker(service, registry, worker_id="worker-1").run(job_id)

    assert service.get_job(job_id).status is JobStatus.CANCELLED


def test_non_cancellable_phase_defers_cancel_until_later_checkpoint(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job_id = _create(service, "deferred-cancel")
    service.transition(job_id, JobStatus.RUNNING, worker_id="worker-1")
    context = WorkerContext(service, job_id)

    with context.non_cancellable("atomic publish"):
        service.request_cancel(job_id, reason="pytest")
        context.checkpoint()

    with pytest.raises(JobCancelledAtCheckpoint):
        context.checkpoint()


def test_handler_exception_persists_structured_failure_and_is_re_raised(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job_id = _create(service, "failure")
    registry = WorkerRegistry()

    secret = "top-secret-token"

    def handler(context: object) -> None:
        raise RuntimeError(f"password={secret}")

    registry.register("synthetic", handler)
    with pytest.raises(RuntimeError, match=secret):
        Worker(service, registry, worker_id="worker-1").run(job_id)

    failed = service.get_job(job_id)
    assert failed.status is JobStatus.FAILED
    assert failed.failure is not None
    assert failed.failure.error_type == "RuntimeError"
    assert secret not in failed.failure.message
    with sqlite3.connect(tmp_path / "worker.db") as connection:
        stored = connection.execute(
            "SELECT failure_json FROM job_runs WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    assert secret not in stored
