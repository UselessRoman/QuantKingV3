from __future__ import annotations

import multiprocessing
import time
from pathlib import Path
from queue import Empty

import pytest

from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    create_sqlite_engine,
    upgrade_database,
)
from quantking.jobs.models import JobStatus
from quantking.jobs.services import CreateJob, JobService

PROCESS_COUNT = 4
TRANSACTIONS_PER_PROCESS = 25
EXPECTED_TRANSACTIONS = PROCESS_COUNT * TRANSACTIONS_PER_PROCESS


def _spawn_writer(database_text: str, job_id: str, worker_index: int, result_queue: object) -> None:
    database = Path(database_text)
    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    service = JobService(SqliteUnitOfWorkFactory(engine))
    try:
        for sequence in range(TRANSACTIONS_PER_PROCESS):
            service.record_activity(
                job_id,
                worker_id=f"spawn-{worker_index}",
                stage="concurrency",
                progress=(sequence + 1) / TRANSACTIONS_PER_PROCESS,
                summary=f"worker={worker_index};sequence={sequence}",
                audit_action="concurrency_pulse",
            )
        result_queue.put((worker_index, None))
    except BaseException as error:  # pragma: no cover - reported in the parent process
        result_queue.put((worker_index, f"{type(error).__name__}: {error}"))
        raise
    finally:
        engine.dispose()


def _spawn_claim(database_text: str, job_id: str, worker_index: int, result_queue: object) -> None:
    database = Path(database_text)
    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    service = JobService(SqliteUnitOfWorkFactory(engine))
    try:
        service.transition(job_id, JobStatus.RUNNING, worker_id=f"claim-{worker_index}")
    except BaseException as error:
        result_queue.put(type(error).__name__)
    else:
        result_queue.put("claimed")
    finally:
        engine.dispose()


def _cleanup_processes(processes: list[multiprocessing.Process]) -> None:
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)


@pytest.mark.timeout(30)
def test_spawn_processes_commit_100_short_transactions_without_loss(tmp_path: Path) -> None:
    database = tmp_path / "concurrency.db"
    upgrade_database(database)
    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    service = JobService(SqliteUnitOfWorkFactory(engine))
    job = service.create_job(
        CreateJob(
            job_type="synthetic",
            idempotency_key="spawn-gate",
            input_manifest={},
            config_manifest={},
            version_manifest={},
            requester="pytest",
            log_id="logs/spawn.jsonl",
        )
    )
    service.transition(job.job_id, JobStatus.RUNNING, worker_id="coordinator")
    starting_version = service.get_job(job.job_id).row_version
    engine.dispose()

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_spawn_writer,
            args=(str(database), job.job_id, worker_index, result_queue),
        )
        for worker_index in range(PROCESS_COUNT)
    ]
    started = time.monotonic()
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=max(0.1, 25 - (time.monotonic() - started)))
        elapsed = time.monotonic() - started
        print(
            f"S0-03 concurrency gate: processes={PROCESS_COUNT} "
            f"transactions={EXPECTED_TRANSACTIONS} elapsed_seconds={elapsed:.3f}"
        )

        results: list[tuple[int, str | None]] = []
        for _ in processes:
            try:
                results.append(result_queue.get(timeout=1))
            except Empty:
                break

        assert elapsed < 30
        assert [process.exitcode for process in processes] == [0] * PROCESS_COUNT
        assert len(results) == PROCESS_COUNT
        assert all(error is None for _, error in results), results
    finally:
        _cleanup_processes(processes)
        result_queue.cancel_join_thread()
        result_queue.close()

    verification_engine = create_sqlite_engine(database, busy_timeout_ms=250)
    verification = JobService(SqliteUnitOfWorkFactory(verification_engine))
    stored = verification.get_job(job.job_id)
    events = verification.list_events(after_event_id=0, limit=1000)
    activity_events = [event for event in events if event.event_type == "job_activity"]
    audits = verification.list_audits(action="concurrency_pulse", limit=1000)

    assert stored.status is JobStatus.RUNNING
    assert stored.row_version == starting_version + EXPECTED_TRANSACTIONS
    assert len(activity_events) == EXPECTED_TRANSACTIONS
    assert len(audits) == EXPECTED_TRANSACTIONS
    assert len({event.summary for event in activity_events}) == EXPECTED_TRANSACTIONS
    assert [event.event_id for event in events] == sorted(event.event_id for event in events)
    verification_engine.dispose()


@pytest.mark.timeout(30)
def test_two_spawn_workers_cannot_claim_the_same_pending_job(tmp_path: Path) -> None:
    database = tmp_path / "claim.db"
    upgrade_database(database)
    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    service = JobService(SqliteUnitOfWorkFactory(engine))
    job = service.create_job(
        CreateJob(
            job_type="synthetic",
            idempotency_key="claim-race",
            input_manifest={},
            config_manifest={},
            version_manifest={},
            requester="pytest",
            log_id="logs/claim.jsonl",
        )
    )
    engine.dispose()

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    processes = [
        context.Process(target=_spawn_claim, args=(str(database), job.job_id, index, result_queue))
        for index in range(2)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
        results = sorted(result_queue.get(timeout=1) for _ in processes)
        assert [process.exitcode for process in processes] == [0, 0]
        assert results == ["InvalidJobTransitionError", "claimed"]
    finally:
        _cleanup_processes(processes)
        result_queue.cancel_join_thread()
        result_queue.close()

    verification_engine = create_sqlite_engine(database, busy_timeout_ms=250)
    verification = JobService(SqliteUnitOfWorkFactory(verification_engine))
    assert verification.get_job(job.job_id).status is JobStatus.RUNNING
    assert (
        sum(
            event.event_type == "job_running"
            for event in verification.list_events(after_event_id=0, limit=100)
        )
        == 1
    )
    verification_engine.dispose()
