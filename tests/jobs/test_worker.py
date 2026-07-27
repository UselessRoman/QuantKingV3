from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quantking.common.errors import InvalidJobTransitionError, UpstreamJobNotReadyError
from quantking.common.time import UtcTimestamp
from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    create_sqlite_engine,
    initialize_database,
)
from quantking.jobs.models import JobStatus
from quantking.jobs.services import CreateJob, JobService
from quantking.jobs.worker import JobCancelledAtCheckpoint, Worker, WorkerContext, WorkerRegistry


def _service(tmp_path: Path) -> JobService:
    database = tmp_path / "worker.db"
    initialize_database(database)
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


def test_handler_return_value_is_rejected_and_never_leaves_running(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job_id = _create(service, "bad-return")
    registry = WorkerRegistry()
    registry.register("synthetic", lambda context: {"not": "serializable result"})

    with pytest.raises(TypeError, match="return None"):
        Worker(service, registry, worker_id="worker-1").run(job_id)

    assert service.get_job(job_id).status is JobStatus.FAILED


def test_two_workers_competing_for_one_job_execute_handler_once(tmp_path: Path) -> None:
    service = _service(tmp_path)
    job_id = _create(service, "two-workers")
    registry = WorkerRegistry()
    barrier = threading.Barrier(2)
    handler_started = threading.Event()
    release_handler = threading.Event()
    calls = 0
    lock = threading.Lock()

    def handler(context: WorkerContext) -> None:
        del context
        nonlocal calls
        with lock:
            calls += 1
        handler_started.set()
        assert release_handler.wait(timeout=5)

    def run(worker_id: str) -> BaseException | None:
        barrier.wait(timeout=5)
        try:
            Worker(service, registry, worker_id=worker_id).run(job_id)
        except BaseException as error:
            return error
        return None

    registry.register("synthetic", handler)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, f"worker-{index}") for index in range(2)]
        assert handler_started.wait(timeout=5)
        release_handler.set()
        outcomes = [future.result(timeout=5) for future in futures]

    assert calls == 1
    assert service.get_job(job_id).status is JobStatus.SUCCEEDED
    assert sum(isinstance(item, InvalidJobTransitionError) for item in outcomes) == 1


def test_active_long_handler_renews_lease_across_stale_threshold(tmp_path: Path) -> None:
    database = tmp_path / "long-lease.db"
    initialize_database(database)
    now = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)
    service = JobService(
        SqliteUnitOfWorkFactory(create_sqlite_engine(database, busy_timeout_ms=250)),
        clock=lambda: now,
    )
    job_id = _create(service, "long-lease")
    registry = WorkerRegistry()

    class ManualLeaseRenewer:
        callback = None

        @contextmanager
        def maintain(self, callback):
            self.callback = callback
            callback()
            yield

        def tick(self) -> None:
            assert self.callback is not None
            self.callback()

    renewer = ManualLeaseRenewer()

    def handler(context: WorkerContext) -> None:
        del context
        nonlocal now
        now += timedelta(minutes=3)
        renewer.tick()
        assert (
            service.recover_stale_running_jobs(
                reason="must not recover active lease",
                stale_before=UtcTimestamp(now - timedelta(minutes=2)),
                at=UtcTimestamp(now),
            )
            == 0
        )

    registry.register("synthetic", handler)
    Worker(
        service,
        registry,
        worker_id="worker-lease",
        lease_renewer=renewer,
    ).run(job_id)

    assert service.get_job(job_id).status is JobStatus.SUCCEEDED


def test_worker_stops_lease_before_terminal_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "terminal-after-lease.db"
    initialize_database(database)
    service = JobService(
        SqliteUnitOfWorkFactory(create_sqlite_engine(database, busy_timeout_ms=250))
    )
    job_id = _create(service, "terminal-after-lease")
    registry = WorkerRegistry()
    registry.register("synthetic", lambda context: None)

    class ObservedRenewer:
        active = False

        @contextmanager
        def maintain(self, callback):
            del callback
            self.active = True
            try:
                yield
            finally:
                self.active = False

    renewer = ObservedRenewer()
    original_transition = service.transition

    def checked_transition(job_id_value, target, **kwargs):
        if target in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }:
            assert not renewer.active
        return original_transition(job_id_value, target, **kwargs)

    monkeypatch.setattr(service, "transition", checked_transition)
    Worker(
        service,
        registry,
        worker_id="terminal-order-worker",
        lease_renewer=renewer,
    ).run(job_id)

    assert service.get_job(job_id).status is JobStatus.SUCCEEDED


def test_lost_lease_cannot_commit_success(tmp_path: Path) -> None:
    database = tmp_path / "lost-lease.db"
    initialize_database(database)
    service = JobService(
        SqliteUnitOfWorkFactory(create_sqlite_engine(database, busy_timeout_ms=250))
    )
    job_id = _create(service, "lost-lease")
    registry = WorkerRegistry()
    registry.register("synthetic", lambda context: None)

    class LostRenewer:
        @contextmanager
        def maintain(self, callback):
            del callback
            yield
            raise RuntimeError("synthetic lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        Worker(
            service,
            registry,
            worker_id="lost-lease-worker",
            lease_renewer=LostRenewer(),
        ).run(job_id)

    assert service.get_job(job_id).status is JobStatus.RUNNING
