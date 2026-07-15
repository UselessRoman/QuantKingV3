from __future__ import annotations

from datetime import UTC, datetime

import pytest

from quantking.common.errors import InvalidJobTransitionError
from quantking.common.time import UtcTimestamp
from quantking.jobs.models import (
    FailureInfo,
    JobRun,
    JobStatus,
    RetryPolicy,
    transition_job,
)


def _pending_job() -> JobRun:
    return JobRun.pending(
        job_id="job:9ba3f815-8ec0-4aeb-8927-04fa033f81af",
        job_type="synthetic",
        idempotency_key="weekly-fixture",
        input_manifest={"input": "synthetic-v1"},
        config_manifest={"config": "default-v1"},
        version_manifest={"code": "test"},
        requester="pytest",
        created_at=UtcTimestamp(datetime(2026, 7, 15, 1, 2, tzinfo=UTC)),
        log_id="logs/jobs/synthetic.jsonl",
    )


def test_job_state_machine_allows_only_centralized_transitions() -> None:
    pending = _pending_job()
    running = transition_job(
        pending,
        JobStatus.RUNNING,
        at=UtcTimestamp(datetime(2026, 7, 15, 1, 3, tzinfo=UTC)),
        worker_id="worker-1",
    )

    assert running.status is JobStatus.RUNNING
    assert running.started_at is not None
    assert running.worker_id == "worker-1"
    assert running.row_version == pending.row_version + 1

    for terminal in (
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
    ):
        result = transition_job(
            running,
            terminal,
            at=UtcTimestamp(datetime(2026, 7, 15, 1, 4, tzinfo=UTC)),
            failure=(
                FailureInfo(error_type="SyntheticError", message="failed", retryable=False)
                if terminal in {JobStatus.FAILED, JobStatus.INTERRUPTED}
                else None
            ),
        )
        assert result.status is terminal
        assert result.ended_at is not None


def test_pending_may_be_cancelled_but_terminal_states_never_transition() -> None:
    pending = _pending_job()
    cancelled = transition_job(
        pending,
        JobStatus.CANCELLED,
        at=UtcTimestamp(datetime(2026, 7, 15, 1, 3, tzinfo=UTC)),
        cancel_result="cancelled before claim",
    )

    assert cancelled.cancel_result == "cancelled before claim"
    with pytest.raises(InvalidJobTransitionError):
        transition_job(
            cancelled,
            JobStatus.RUNNING,
            at=UtcTimestamp(datetime(2026, 7, 15, 1, 4, tzinfo=UTC)),
        )


def test_job_run_rejects_progress_outside_closed_unit_interval() -> None:
    with pytest.raises(ValueError, match="progress"):
        JobRun.pending(
            job_id="job:9ba3f815-8ec0-4aeb-8927-04fa033f81af",
            job_type="synthetic",
            idempotency_key="invalid-progress",
            input_manifest={},
            config_manifest={},
            version_manifest={},
            requester="pytest",
            created_at=UtcTimestamp(datetime(2026, 7, 15, 1, 2, tzinfo=UTC)),
            log_id="logs/jobs/invalid.jsonl",
            progress=1.1,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_attempts": 21},
        {"base_delay_ms": 5001, "cap_delay_ms": 5001},
        {"cap_delay_ms": 5001},
        {"heartbeat_min_interval_ms": 60_001},
        {"progress_min_interval_ms": 60_001},
    ],
)
def test_retry_policy_rejects_values_that_defeat_bounded_waits(
    overrides: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**overrides)  # type: ignore[arg-type]
