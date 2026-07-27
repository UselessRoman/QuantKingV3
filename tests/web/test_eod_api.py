from __future__ import annotations

import io
import json
from pathlib import Path

from fastapi.testclient import TestClient

from quantking.cli.eod import run_cli
from quantking.data.eod.contracts import EodRunRequest
from quantking.data.eod.service import EodCommandService, EodExecutionConfig
from quantking.jobs.models import JobStatus
from quantking.jobs.services import JobService
from quantking.web.app import create_app


def _command(jobs: JobService) -> EodCommandService:
    return EodCommandService(
        jobs,
        config=EodExecutionConfig.production(code_version="test-code"),
    )


def _body(*, day: str = "2026-07-15") -> dict[str, object]:
    return {
        "start_date": day,
        "end_date": day,
        "datasets": ["exchange_calendar"],
        "publish_intent": "PUBLISH_AFTER_QUALITY",
    }


def test_post_enqueues_only_returns_202_and_idempotently_replays(
    job_service: JobService,
) -> None:
    command = _command(job_service)
    app = create_app(
        job_service,
        eod_command_port=command,
        job_command_port=job_service,
    )
    with TestClient(app) as client:
        first = client.post(
            "/api/data/eod-runs",
            json=_body(),
            headers={"Idempotency-Key": "api-key"},
        )
        replay = client.post(
            "/api/data/eod-runs",
            json=_body(),
            headers={"Idempotency-Key": "api-key"},
        )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert first.json()["job_id"] == replay.json()["job_id"]
    assert first.json()["created"] is True
    assert replay.json()["created"] is False
    assert first.headers["location"] == f"/api/jobs/{first.json()['job_id']}"
    assert job_service.get_job(first.json()["job_id"]).status is JobStatus.PENDING


def test_post_rejects_missing_key_unknown_fields_and_conflicting_replay(
    job_service: JobService,
) -> None:
    command = _command(job_service)
    app = create_app(job_service, eod_command_port=command)
    with TestClient(app) as client:
        missing = client.post("/api/data/eod-runs", json=_body())
        unknown = client.post(
            "/api/data/eod-runs",
            json={**_body(), "job_type": "evil"},
            headers={"Idempotency-Key": "unknown-field"},
        )
        client.post(
            "/api/data/eod-runs",
            json=_body(),
            headers={"Idempotency-Key": "conflict"},
        )
        conflict = client.post(
            "/api/data/eod-runs",
            json=_body(day="2026-07-16"),
            headers={"Idempotency-Key": "conflict"},
        )

    assert missing.status_code == 422
    assert unknown.status_code == 422
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert "manifest" not in conflict.text.casefold()
    assert "path" not in conflict.text.casefold()


def test_cancel_requires_and_checks_row_version(job_service: JobService) -> None:
    command = _command(job_service)
    created = command.enqueue(
        EodRunRequest.from_dict(
            {
                "schema_version": "eod-request.v1",
                "start_date": "2026-07-15",
                "end_date": "2026-07-15",
                "datasets": ["exchange_calendar"],
                "publish_intent": "PUBLISH_AFTER_QUALITY",
                "source": "XTQUANT",
            }
        ),
        idempotency_key="cancel-api",
        requester="pytest",
    )
    app = create_app(job_service, job_command_port=job_service)
    with TestClient(app) as client:
        missing = client.post(
            f"/api/jobs/{created.job_id}/cancel",
            json={"reason": "user request"},
        )
        stale = client.post(
            f"/api/jobs/{created.job_id}/cancel",
            json={"reason": "user request", "expected_row_version": 99},
        )
        cancelled = client.post(
            f"/api/jobs/{created.job_id}/cancel",
            json={"reason": "user request", "expected_row_version": created.row_version},
        )

    assert missing.status_code == 400
    assert stale.status_code == 409
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"


def test_route_does_not_touch_gateway_or_worker(
    job_service: JobService,
    tmp_path: Path,
) -> None:
    del tmp_path
    command = _command(job_service)
    with TestClient(create_app(job_service, eod_command_port=command)) as client:
        response = client.post(
            "/api/data/eod-runs",
            json=_body(),
            headers={"Idempotency-Key": "enqueue-only"},
        )
    assert response.status_code == 202
    assert job_service.get_job(response.json()["job_id"]).status is JobStatus.PENDING


def test_cancel_returns_conflict_inside_non_cancellable_phase(
    job_service: JobService,
) -> None:
    command = _command(job_service)
    created = command.enqueue(
        EodRunRequest.from_dict(
            {
                "schema_version": "eod-request.v1",
                "start_date": "2026-07-15",
                "end_date": "2026-07-15",
                "datasets": ["exchange_calendar"],
                "publish_intent": "PUBLISH_AFTER_QUALITY",
                "source": "XTQUANT",
            }
        ),
        idempotency_key="non-cancellable-api",
        requester="pytest",
    )
    job_service.transition(created.job_id, JobStatus.RUNNING, worker_id="worker")
    critical = job_service.set_non_cancellable_phase(
        created.job_id,
        phase="canonical_publish",
    )
    with TestClient(create_app(job_service, job_command_port=job_service)) as client:
        response = client.post(
            f"/api/jobs/{created.job_id}/cancel",
            json={
                "reason": "user request",
                "expected_row_version": critical.row_version,
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_conflict"


def test_cli_and_api_freeze_identical_request_manifests(
    job_service: JobService,
) -> None:
    command = _command(job_service)
    output = io.StringIO()

    assert (
        run_cli(
            [
                "eod-enqueue",
                "--trade-date",
                "2026-07-15",
                "--datasets",
                "exchange_calendar",
                "--idempotency-key",
                "cli-manifest",
                "--publish-intent",
                "PUBLISH_AFTER_QUALITY",
            ],
            eod_commands=command,
            worker=type("NoWorker", (), {"run": lambda self, job_id: None})(),
            jobs=job_service,
            stdout=output,
        )
        == 0
    )
    cli_job = job_service.get_job(json.loads(output.getvalue())["job_id"])
    with TestClient(create_app(job_service, eod_command_port=command)) as client:
        response = client.post(
            "/api/data/eod-runs",
            json=_body(),
            headers={"Idempotency-Key": "api-manifest"},
        )
    api_job = job_service.get_job(response.json()["job_id"])

    assert cli_job.input_manifest == api_job.input_manifest
    assert cli_job.config_manifest == api_job.config_manifest
    assert cli_job.version_manifest == api_job.version_manifest
