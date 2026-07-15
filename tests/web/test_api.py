from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quantking.common.time import Clock
from quantking.jobs.services import JobService
from quantking.web.app import create_app


def test_health_and_job_snapshot_use_stable_public_schemas(
    job_service: JobService,
    populated_job: str,
    fixed_clock: Clock,
) -> None:
    with TestClient(create_app(job_service, clock=fixed_clock)) as client:
        health = client.get("/api/health")
        job = client.get(f"/api/jobs/{populated_job}")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "service": "quantking-web",
        "checked_at": "2026-07-15T04:05:06Z",
    }
    assert job.status_code == 200
    assert job.json()["job_id"] == populated_job
    assert job.json()["status"] == "RUNNING"
    assert job.json()["progress"] == 0.5
    assert job.json()["stage"] == "synthetic-step"
    assert "input_manifest" not in job.json()
    assert "config_manifest" not in job.json()


def test_missing_job_and_validation_errors_do_not_leak_internal_details(
    job_service: JobService,
) -> None:
    with TestClient(create_app(job_service)) as client:
        missing = client.get("/api/jobs/job:00000000-0000-4000-8000-000000000000")
        invalid_type = client.get("/api/job-events?after_event_id=secret&limit=10")
        invalid_range = client.get("/api/job-events?after_event_id=0&limit=1001")

    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "job_not_found", "message": "任务不存在。"}}
    assert invalid_type.status_code == 422
    assert invalid_type.json() == {
        "error": {"code": "validation_error", "message": "请求参数无效。"}
    }
    assert "secret" not in invalid_type.text
    assert invalid_range.status_code == 400
    assert invalid_range.json() == {
        "error": {"code": "invalid_request", "message": "事件查询范围无效。"}
    }


@pytest.mark.parametrize(
    ("query", "status"),
    [
        ("after_event_id=-1&limit=10", 400),
        ("after_event_id=0&limit=0", 400),
        ("after_event_id=0&limit=1001", 400),
    ],
)
def test_event_query_enforces_cursor_bounds(
    job_service: JobService,
    query: str,
    status: int,
) -> None:
    with TestClient(create_app(job_service)) as client:
        response = client.get(f"/api/job-events?{query}")
    assert response.status_code == status


def test_event_query_is_ordered_and_strictly_after_cursor(
    job_service: JobService,
    populated_job: str,
) -> None:
    del populated_job
    with TestClient(create_app(job_service)) as client:
        first = client.get("/api/job-events?after_event_id=0&limit=2")
        cursor = first.json()["next_cursor"]
        second = client.get(f"/api/job-events?after_event_id={cursor}&limit=10")

    first_ids = [event["event_id"] for event in first.json()["events"]]
    second_ids = [event["event_id"] for event in second.json()["events"]]
    assert first_ids == sorted(first_ids)
    assert second_ids == sorted(second_ids)
    assert all(event_id > cursor for event_id in second_ids)
    assert len(first_ids) == 2


def test_api_does_not_enable_cross_origin_access(job_service: JobService) -> None:
    with TestClient(create_app(job_service)) as client:
        response = client.get(
            "/api/health",
            headers={"Origin": "https://not-local.example"},
        )
    assert "access-control-allow-origin" not in response.headers


def test_static_frontend_and_spa_fallback_never_swallow_api(
    job_service: JobService,
    tmp_path: Path,
) -> None:
    static_root = tmp_path / "dist"
    assets = static_root / "assets"
    assets.mkdir(parents=True)
    (static_root / "index.html").write_text("<main>中文壳</main>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('ok')", encoding="utf-8")

    with TestClient(create_app(job_service, static_root=static_root.resolve())) as client:
        root = client.get("/")
        route = client.get("/jobs/example")
        asset = client.get("/assets/app.js")
        unknown_api = client.get("/api/not-a-route")
        directory = client.get("/assets/")

    assert root.text == "<main>中文壳</main>"
    assert route.text == root.text
    assert asset.text == "console.log('ok')"
    assert unknown_api.status_code == 404
    assert unknown_api.headers["content-type"].startswith("application/json")
    assert directory.status_code == 404


@pytest.mark.parametrize("relative", ["missing", "file.txt"])
def test_invalid_static_root_fails_at_app_construction(
    job_service: JobService,
    tmp_path: Path,
    relative: str,
) -> None:
    target = tmp_path / relative
    if target.suffix:
        target.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="static root"):
        create_app(job_service, static_root=target.resolve())


def test_relative_static_root_and_missing_index_are_rejected(
    job_service: JobService,
    tmp_path: Path,
) -> None:
    empty_root = tmp_path / "empty-dist"
    empty_root.mkdir()

    with pytest.raises(ValueError, match="absolute"):
        create_app(job_service, static_root=Path("frontend/dist"))
    with pytest.raises(ValueError, match=r"index\.html"):
        create_app(job_service, static_root=empty_root.resolve())
