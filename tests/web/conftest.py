from __future__ import annotations

import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from quantking.common.time import Clock
from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    create_sqlite_engine,
    initialize_database,
)
from quantking.jobs.models import JobStatus
from quantking.jobs.services import CreateJob, JobService

_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex


@pytest.fixture(autouse=True)
def allow_event_loop_loopback(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Permit only the loopback socketpair that TestClient/asyncio require."""

    def connect(client: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and address and address[0] in {"127.0.0.1", "::1"}:
            return _REAL_CONNECT(client, address)
        raise RuntimeError("Web tests cannot access external network")

    def connect_ex(client: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and address and address[0] in {"127.0.0.1", "::1"}:
            return _REAL_CONNECT_EX(client, address)
        raise RuntimeError("Web tests cannot access external network")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield


@pytest.fixture
def fixed_clock() -> Clock:
    return lambda: datetime(2026, 7, 15, 4, 5, 6, tzinfo=UTC)


@pytest.fixture
def job_service(tmp_path: Path, fixed_clock: Clock) -> JobService:
    database = tmp_path / "web.db"
    initialize_database(database)
    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    return JobService(SqliteUnitOfWorkFactory(engine), clock=fixed_clock)


@pytest.fixture
def populated_job(job_service: JobService) -> str:
    job = job_service.create_job(
        CreateJob(
            job_type="synthetic",
            idempotency_key="web-shell-fixture",
            input_manifest={"input": "synthetic-v1"},
            config_manifest={"config": "default-v1"},
            version_manifest={"code": "test"},
            requester="pytest",
            log_id="logs/jobs/web-shell.jsonl",
        )
    )
    job_service.transition(job.job_id, JobStatus.RUNNING, worker_id="worker-1")
    job_service.report_progress(
        job.job_id,
        stage="synthetic-step",
        progress=0.5,
        summary="halfway",
    )
    return job.job_id
