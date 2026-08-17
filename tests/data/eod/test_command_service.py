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
            config=EodExecutionConfig.production_k4(
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
        "resolution_rule_version": "source-resolution.daily-bar-unadjusted.k4.v1",
        "source_route_version": "BAO-TX-SINA.k4.v1",
        "baostock_version": "0.9.3",
        "baostock_unit_policy_version": "test-baostock-units",
        "akshare_unit_policy_version": "test-akshare-units",
        "planner_lookback_days": 0,
        "adapter_context_lookback_days": 15,
        "max_days_per_chunk": 31,
        "max_items_per_chunk": 1,
        "endpoint_invocation_limit": 12,
        "c_endpoint_invocation_limit": 4,
        "d_endpoint_invocation_limit": 0,
        "estimated_http_request_limit": 20,
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


def test_production_config_is_r2_capped_and_freezes_roundtrip(tmp_path: Path) -> None:
    from quantking.data.eod.contracts import R2_EOD_CONFIG_SCHEMA
    from quantking.data.eod.frozen import parse_frozen_eod_command
    from quantking.data.eod.release_manifest import RELEASE_SAFETY_CAPS

    database = tmp_path / "jobs-r2.db"
    initialize_database(database)
    jobs = JobService(SqliteUnitOfWorkFactory(create_sqlite_engine(database, busy_timeout_ms=250)))
    config = EodExecutionConfig.production(code_version="test-r2", settings=app_settings(tmp_path))
    assert config.is_r2
    cap = RELEASE_SAFETY_CAPS["endpoint_invocations_per_role"]
    assert config.a_baostock_endpoint_limit == cap
    assert config.b_tencent_endpoint_limit == cap
    assert config.endpoint_invocation_limit == cap * 3
    assert config.estimated_http_request_limit == cap * 3 + cap * 2
    assert config.release_manifest_sha256 is None
    assert config.legacy_import_manifest_sha256 is None

    rendered = config.to_dict(selected=("daily_bar_unadjusted",), bases={})
    assert rendered["schema_version"] == R2_EOD_CONFIG_SCHEMA
    assert rendered["release_manifest_sha256"] is None
    assert rendered["a_baostock_endpoint_limit"] == cap

    command = EodCommandService(jobs, config=config)
    created = command.enqueue(_request(), idempotency_key="r2-cap", requester="pytest")
    job = jobs.get_job(created.job_id)
    assert job.version_manifest["akshare_unit_policy_version"]
    frozen = parse_frozen_eod_command(
        job_type=job.job_type,
        input_manifest=job.input_manifest,
        config_manifest=job.config_manifest,
        version_manifest=job.version_manifest,
    )
    assert frozen.a_baostock_endpoint_limit == cap
    assert frozen.b_tencent_endpoint_limit == cap
    assert frozen.release_manifest_sha256 is None


def test_production_config_freezes_k4_constants_and_manifest(tmp_path: Path) -> None:
    config = EodExecutionConfig.production_k4(
        code_version="test-code", settings=app_settings(tmp_path)
    )
    assert config.adapter_version == "baostock-akshare-daily-bar.k4.v1"
    assert config.resolution_rule_version == "source-resolution.daily-bar-unadjusted.k4.v1"
    assert config.source_route_version == "BAO-TX-SINA.k4.v1"
    assert config.endpoint_invocation_limit == 12
    assert config.c_endpoint_invocation_limit == 4
    assert config.d_endpoint_invocation_limit == 0
    assert config.estimated_http_request_limit == 20
    assert config.estimated_baostock_protocol_message_limit == 12
    assert config.akshare_unit_policy_version

    rendered = config.to_dict(selected=("daily_bar_unadjusted",), bases={})
    assert rendered["akshare_unit_policy_version"] == config.akshare_unit_policy_version
    assert rendered["source_route_version"] == "BAO-TX-SINA.k4.v1"
    assert rendered["endpoint_invocation_limit"] == 12
    assert rendered["c_endpoint_invocation_limit"] == 4
    assert rendered["d_endpoint_invocation_limit"] == 0
    assert rendered["estimated_http_request_limit"] == 20
    assert rendered["estimated_baostock_protocol_message_limit"] == 12

    command, jobs = _service(tmp_path)
    created = command.enqueue(_request(), idempotency_key="k4-manifest", requester="pytest")
    manifest = jobs.get_job(created.job_id).version_manifest
    assert manifest["akshare_unit_policy_version"] == config.akshare_unit_policy_version
    assert manifest["source_route_version"] == "BAO-TX-SINA.k4.v1"


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
        config=EodExecutionConfig.production_k4(
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
