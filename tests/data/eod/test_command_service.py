from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest

from quantking.common.errors import IdempotencyConflictError
from quantking.data.eod.contracts import EodPublishIntent, EodRunRequest
from quantking.data.eod.service import EodCommandService, EodExecutionConfig
from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    create_sqlite_engine,
    initialize_database,
)
from quantking.jobs.services import JobService


def _service(tmp_path: Path) -> tuple[EodCommandService, JobService]:
    database = tmp_path / "jobs.db"
    initialize_database(database)
    jobs = JobService(SqliteUnitOfWorkFactory(create_sqlite_engine(database, busy_timeout_ms=250)))
    return (
        EodCommandService(
            jobs,
            config=EodExecutionConfig.production(code_version="test-code"),
        ),
        jobs,
    )


def _request(*, day: date = date(2026, 7, 15)) -> EodRunRequest:
    return EodRunRequest(
        start_date=day,
        end_date=day,
        datasets=("exchange_calendar",),
        publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
    )


def test_same_key_and_request_reuses_job_and_different_request_conflicts(tmp_path: Path) -> None:
    command, jobs = _service(tmp_path)

    first = command.enqueue(_request(), idempotency_key="same-key", requester="pytest")
    replay = command.enqueue(_request(), idempotency_key="same-key", requester="pytest")

    assert replay.job_id == first.job_id
    assert first.created is True
    assert replay.created is False
    persisted = jobs.get_job(first.job_id)
    assert persisted.input_manifest["request_sha256"]
    assert persisted.input_manifest["request_json"]
    assert persisted.config_manifest["config_sha256"]

    with pytest.raises(IdempotencyConflictError):
        command.enqueue(
            _request(day=date(2026, 7, 16)),
            idempotency_key="same-key",
            requester="pytest",
        )


def test_concurrent_enqueue_creates_one_job_run(tmp_path: Path) -> None:
    command, jobs = _service(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: command.enqueue(
                    _request(),
                    idempotency_key="concurrent-key",
                    requester="pytest",
                ),
                range(16),
            )
        )

    assert len({item.job_id for item in results}) == 1
    assert sum(item.created for item in results) == 1
    assert [event.event_type for event in jobs.list_events(after_event_id=0, limit=100)] == [
        "job_created"
    ]


@pytest.mark.parametrize(
    "key",
    ["latest", "../job", r"C:\private", "password=hunter2", "select * from jobs"],
)
def test_enqueue_rejects_unsafe_idempotency_keys(tmp_path: Path, key: str) -> None:
    command, _jobs = _service(tmp_path)
    with pytest.raises(ValueError, match="idempotency"):
        command.enqueue(_request(), idempotency_key=key, requester="pytest")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lookback_days", True),
        ("max_days_per_chunk", True),
        ("max_items_per_chunk", True),
    ],
)
def test_execution_config_rejects_bool_integer_fields(field: str, value: bool) -> None:
    arguments = {
        "code_version": "test-code",
        "adapter_version": "test-adapter",
        "rule_version": "test-rules",
        "lookback_days": 0,
        "max_days_per_chunk": 1,
        "max_items_per_chunk": 1,
        "datasets": {},
    }
    arguments[field] = value

    with pytest.raises((TypeError, ValueError)):
        EodExecutionConfig(**arguments)  # type: ignore[arg-type]
