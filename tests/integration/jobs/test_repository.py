from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from quantking.common.errors import (
    IdempotencyConflictError,
    InvalidJobTransitionError,
    PersistenceBusyError,
)
from quantking.common.time import UtcTimestamp
from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    create_sqlite_engine,
    initialize_database,
)
from quantking.jobs.models import JobStatus
from quantking.jobs.services import CreateJob, JobService, RetryPolicy


def _service(database: Path, *, policy: RetryPolicy | None = None) -> JobService:
    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    return JobService(SqliteUnitOfWorkFactory(engine), retry_policy=policy or RetryPolicy())


def _create(service: JobService, *, key: str = "same-key", upstream: tuple[str, ...] = ()) -> str:
    job = service.create_job(
        CreateJob(
            job_type="synthetic",
            idempotency_key=key,
            input_manifest={"input": "v1"},
            config_manifest={"config": "v1"},
            version_manifest={"code": "test"},
            requester="pytest",
            log_id=f"logs/{key}.jsonl",
            upstream_job_ids=upstream,
        )
    )
    return job.job_id


def test_idempotent_create_returns_same_job_without_duplicate_event(tmp_path: Path) -> None:
    database = tmp_path / "idempotent.db"
    initialize_database(database)
    service = _service(database)

    first = _create(service)
    second = _create(service)

    assert second == first
    events = service.list_events(after_event_id=0, limit=100)
    assert [event.event_type for event in events] == ["job_created"]


def test_idempotency_key_rejects_a_different_command_without_new_event(tmp_path: Path) -> None:
    database = tmp_path / "idempotency-conflict.db"
    initialize_database(database)
    service = _service(database)
    first = _create(service)

    with pytest.raises(IdempotencyConflictError):
        service.create_job(
            CreateJob(
                job_type="synthetic",
                idempotency_key="same-key",
                input_manifest={"input": "different"},
                config_manifest={"config": "v1"},
                version_manifest={"code": "test"},
                requester="pytest",
                log_id="logs/same-key.jsonl",
            )
        )

    assert service.get_job(first).input_manifest == {"input": "v1"}
    assert [event.event_type for event in service.list_events(after_event_id=0, limit=100)] == [
        "job_created"
    ]


def test_transition_and_event_are_atomic_and_cursor_is_strictly_bounded(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    initialize_database(database)
    service = _service(database)
    job_id = _create(service)

    service.transition(job_id, JobStatus.RUNNING, worker_id="worker-1", summary="claimed")
    before = service.list_events(after_event_id=0, limit=100)
    with pytest.raises(InvalidJobTransitionError):
        service.transition(job_id, JobStatus.PENDING, summary="illegal")
    after = service.list_events(after_event_id=0, limit=100)

    assert service.get_job(job_id).status is JobStatus.RUNNING
    assert after == before
    assert [event.event_id for event in after] == sorted(event.event_id for event in after)
    assert service.list_events(after_event_id=after[-1].event_id, limit=10) == []
    assert service.list_events(after_event_id=0, limit=1) == after[:1]
    with pytest.raises(ValueError, match="limit"):
        service.list_events(after_event_id=0, limit=1001)


def test_event_cursor_is_never_reused_after_cascade_deletes_all_events(tmp_path: Path) -> None:
    database = tmp_path / "monotonic-cursor.db"
    initialize_database(database)
    service = _service(database)
    first_job = _create(service, key="first")
    service.transition(first_job, JobStatus.RUNNING, worker_id="worker-1")
    old_cursor = service.list_events(after_event_id=0, limit=100)[-1].event_id

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM job_runs WHERE job_id = ?", (first_job,))
        connection.commit()
        assert connection.execute("SELECT count(*) FROM job_events").fetchone() == (0,)

    _create(service, key="second")
    new_cursor = service.list_events(after_event_id=0, limit=100)[0].event_id

    assert new_cursor > old_cursor
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'job_events'"
        ).fetchone() == (new_cursor,)


def test_append_only_audit_is_protected_by_database(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    initialize_database(database)
    service = _service(database)
    service.append_audit(
        actor="pytest",
        entity="job:test",
        action="synthetic_action",
        correlation_id="corr:test",
        details={"safe": "value"},
    )

    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("UPDATE audit_records SET action = 'changed'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM audit_records")


def test_recovery_interrupts_legacy_running_jobs_and_emits_event(tmp_path: Path) -> None:
    database = tmp_path / "recovery.db"
    initialize_database(database)
    service = _service(database)
    job_id = _create(service)
    service.transition(
        job_id,
        JobStatus.RUNNING,
        worker_id="dead-worker",
        summary="claimed",
        at=UtcTimestamp(datetime(2026, 7, 15, 1, 0, tzinfo=UTC)),
    )

    assert (
        service.recover_stale_running_jobs(
            reason="worker process restarted",
            stale_before=UtcTimestamp(datetime(2026, 7, 15, 2, 0, tzinfo=UTC)),
            at=UtcTimestamp(datetime(2026, 7, 15, 3, 0, tzinfo=UTC)),
        )
        == 1
    )

    recovered = service.get_job(job_id)
    assert recovered.status is JobStatus.INTERRUPTED
    assert recovered.failure is not None
    assert recovered.failure.error_type == "WorkerInterrupted"
    assert service.list_events(after_event_id=0, limit=100)[-1].event_type == "job_interrupted"


def test_stale_recovery_preserves_fresh_heartbeat(tmp_path: Path) -> None:
    database = tmp_path / "lease-recovery.db"
    initialize_database(database)
    service = _service(database)
    job_id = _create(service)
    started = UtcTimestamp(datetime(2026, 7, 15, 1, 0, tzinfo=UTC))
    service.transition(job_id, JobStatus.RUNNING, worker_id="active-worker", at=started)
    service.heartbeat(job_id, at=UtcTimestamp(started.value + timedelta(seconds=30)))

    recovered = service.recover_stale_running_jobs(
        reason="worker lease expired",
        stale_before=UtcTimestamp(started.value + timedelta(seconds=20)),
        at=UtcTimestamp(started.value + timedelta(seconds=31)),
    )

    assert recovered == 0
    assert service.get_job(job_id).status is JobStatus.RUNNING


def test_heartbeat_and_stale_recovery_race_has_one_legal_winner(tmp_path: Path) -> None:
    database = tmp_path / "lease-race.db"
    initialize_database(database)
    heartbeat_service = _service(database)
    recovery_service = _service(database)
    job_id = _create(heartbeat_service)
    started = UtcTimestamp(datetime(2026, 7, 15, 1, 0, tzinfo=UTC))
    heartbeat_service.transition(
        job_id,
        JobStatus.RUNNING,
        worker_id="racing-worker",
        at=started,
    )
    barrier = threading.Barrier(2)
    current = UtcTimestamp(started.value + timedelta(minutes=3))

    def heartbeat() -> bool:
        barrier.wait(timeout=5)
        return heartbeat_service.heartbeat(job_id, at=current)

    def recover() -> int:
        barrier.wait(timeout=5)
        return recovery_service.recover_stale_running_jobs(
            reason="racing stale recovery",
            stale_before=UtcTimestamp(started.value + timedelta(minutes=2)),
            at=current,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        heartbeat_result = pool.submit(heartbeat)
        recovery_result = pool.submit(recover)
        renewed = heartbeat_result.result(timeout=10)
        recovered = recovery_result.result(timeout=10)

    job = heartbeat_service.get_job(job_id)
    assert (renewed, recovered, job.status) in {
        (True, 0, JobStatus.RUNNING),
        (False, 1, JobStatus.INTERRUPTED),
    }


def test_heartbeat_and_duplicate_progress_are_rate_limited(tmp_path: Path) -> None:
    database = tmp_path / "rate-limit.db"
    initialize_database(database)
    service = _service(database)
    job_id = _create(service)
    base = UtcTimestamp(datetime(2026, 7, 15, 1, 0, tzinfo=UTC))
    service.transition(job_id, JobStatus.RUNNING, worker_id="worker", at=base)

    assert service.heartbeat(job_id, at=base) is False
    assert service.heartbeat(job_id, at=UtcTimestamp(base.value + timedelta(seconds=1))) is True
    assert service.report_progress(job_id, stage="step", progress=0.25, summary="same", at=base)
    assert not service.report_progress(
        job_id,
        stage="step",
        progress=0.25,
        summary="same",
        at=UtcTimestamp(base.value + timedelta(milliseconds=100)),
    )


def test_explicit_write_lock_exhausts_retry_and_raises_stable_error(tmp_path: Path) -> None:
    database = tmp_path / "locked.db"
    initialize_database(database)
    policy = RetryPolicy(
        max_attempts=3,
        base_delay_ms=5,
        cap_delay_ms=10,
        jitter_ratio=0.0,
        heartbeat_min_interval_ms=1000,
        progress_min_interval_ms=250,
    )
    service = _service(database, policy=policy)
    lock = sqlite3.connect(database, timeout=0, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(PersistenceBusyError):
            service.append_audit(
                actor="pytest",
                entity="job:test",
                action="blocked",
                correlation_id="corr:lock",
                details={},
            )
    finally:
        lock.rollback()
        lock.close()

    assert time.monotonic() - started < 3


def test_non_lock_operational_error_is_not_retried_or_rewritten(tmp_path: Path) -> None:
    database = tmp_path / "non-lock.db"
    initialize_database(database)
    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    service = JobService(SqliteUnitOfWorkFactory(engine))
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE job_events"))
    connections = 0

    def count_connection(dbapi_connection: object, connection_record: object) -> None:
        del dbapi_connection, connection_record
        nonlocal connections
        connections += 1

    event.listen(engine, "connect", count_connection)
    with pytest.raises(OperationalError) as caught:
        service.list_events(after_event_id=0, limit=10)

    assert "no such table" in str(caught.value.orig).casefold()
    assert connections == 1
