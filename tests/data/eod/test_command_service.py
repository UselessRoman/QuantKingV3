from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest
from tests.helpers import app_settings

from quantking.common.errors import IdempotencyConflictError
from quantking.data.eod.contracts import EodPublishIntent, EodRunRequest
from quantking.data.eod.service import EodCommandService, EodExecutionConfig
from quantking.jobs.adapters.persistence.sqlite import (
    SqliteUnitOfWorkFactory,
    create_sqlite_engine,
    initialize_database,
)
from quantking.jobs.models import FailureInfo, JobStatus
from quantking.jobs.services import JobService


def _service(tmp_path: Path) -> tuple[EodCommandService, JobService]:
    database = tmp_path / "jobs.db"
    initialize_database(database)
    jobs = JobService(SqliteUnitOfWorkFactory(create_sqlite_engine(database, busy_timeout_ms=250)))
    return (
        EodCommandService(
            jobs,
            config=EodExecutionConfig.production(
                code_version="test-code", settings=app_settings(tmp_path)
            ),
        ),
        jobs,
    )


def _request(*, day: date = date(2026, 7, 15)) -> EodRunRequest:
    return EodRunRequest(
        start_date=day,
        end_date=day,
        datasets=("daily_bar_unadjusted",),
        publish_intent=EodPublishIntent.PUBLISH_AFTER_QUALITY,
        base_versions={},
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
        ("planner_lookback_days", True),
        ("max_days_per_chunk", True),
        ("max_items_per_chunk", True),
    ],
)
def test_execution_config_rejects_bool_integer_fields(field: str, value: bool) -> None:
    arguments = {
        "code_version": "test-code",
        "adapter_version": "test-adapter",
        "rule_version": "test-rules",
        "resolution_rule_version": "source-resolution.daily-bar-unadjusted.k3.v1",
        "source_route_version": "test-route",
        "baostock_version": "0.9.3",
        "baostock_unit_policy_version": "test-baostock-units",
        "planner_lookback_days": 0,
        "adapter_context_lookback_days": 15,
        "max_days_per_chunk": 1,
        "max_items_per_chunk": 1,
        "endpoint_invocation_limit": 16,
        "c_endpoint_invocation_limit": 4,
        "d_endpoint_invocation_limit": 4,
        "estimated_http_request_limit": 24,
        "estimated_baostock_protocol_message_limit": 12,
        "legacy_import_manifest_sha256": None,
        "datasets": {},
    }
    arguments[field] = value

    with pytest.raises((TypeError, ValueError)):
        EodExecutionConfig(**arguments)  # type: ignore[arg-type]


def _finish(jobs: JobService, job_id: str, status: JobStatus) -> None:
    jobs.transition(job_id, JobStatus.RUNNING, worker_id="test-worker", summary="claimed")
    if status is JobStatus.SUCCEEDED:
        jobs.transition(job_id, status, summary="done")
    elif status in {JobStatus.FAILED, JobStatus.INTERRUPTED}:
        jobs.transition(
            job_id,
            status,
            failure=FailureInfo("TEST_FAILURE", "stable failure", False),
            summary="terminal",
        )


@pytest.mark.parametrize("terminal", [JobStatus.FAILED, JobStatus.INTERRUPTED])
def test_retry_creates_new_claimable_job_with_exact_frozen_link(
    tmp_path: Path,
    terminal: JobStatus,
) -> None:
    command, jobs = _service(tmp_path)
    previous = command.enqueue(_request(), idempotency_key="previous", requester="pytest")
    _finish(jobs, previous.job_id, terminal)

    retry = command.enqueue(
        _request(),
        idempotency_key=f"retry-{terminal.value.lower()}",
        requester="pytest",
        retry_of_job_id=previous.job_id,
    )

    assert retry.job_id != previous.job_id
    persisted = jobs.get_job(retry.job_id)
    assert persisted.status is JobStatus.PENDING
    assert persisted.input_manifest["retry_of_job_id"] == previous.job_id
    assert persisted.upstream_job_ids == ()


@pytest.mark.parametrize("terminal", [JobStatus.RUNNING, JobStatus.SUCCEEDED])
def test_retry_rejects_nonfailed_source_job(tmp_path: Path, terminal: JobStatus) -> None:
    command, jobs = _service(tmp_path)
    previous = command.enqueue(_request(), idempotency_key="previous", requester="pytest")
    _finish(jobs, previous.job_id, terminal)

    with pytest.raises(ValueError, match="failed or interrupted"):
        command.enqueue(
            _request(),
            idempotency_key=f"retry-{terminal.value.lower()}",
            requester="pytest",
            retry_of_job_id=previous.job_id,
        )


def test_retry_rejects_changed_request_or_execution_versions(tmp_path: Path) -> None:
    command, jobs = _service(tmp_path)
    previous = command.enqueue(_request(), idempotency_key="previous", requester="pytest")
    _finish(jobs, previous.job_id, JobStatus.FAILED)

    with pytest.raises(ValueError, match="exactly match"):
        command.enqueue(
            _request(day=date(2026, 7, 16)),
            idempotency_key="changed-range",
            requester="pytest",
            retry_of_job_id=previous.job_id,
        )

    changed = EodCommandService(
        jobs,
        config=EodExecutionConfig.production(
            code_version="changed-code", settings=app_settings(tmp_path)
        ),
    )
    with pytest.raises(ValueError, match="exactly match"):
        changed.enqueue(
            _request(),
            idempotency_key="changed-config",
            requester="pytest",
            retry_of_job_id=previous.job_id,
        )
