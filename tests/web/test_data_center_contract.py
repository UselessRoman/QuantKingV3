from __future__ import annotations

from fastapi.testclient import TestClient

from quantking.data.center import DataCenterReadIntegrityError
from quantking.web.app import create_app
from quantking.web.data_center import DataCenterPage


class _Jobs:
    def get_job(self, job_id: str) -> object:
        raise AssertionError(job_id)

    def list_events(self, *, after_event_id: int, limit: int) -> list[object]:
        del after_event_id, limit
        return []


class _DataCenter:
    def summary(self) -> dict[str, object]:
        return {
            "datasets": [],
            "failures": 0,
            "generated_at": "2026-07-28T00:00:00Z",
        }

    def list_batches(self, **filters: object) -> DataCenterPage:
        assert filters["limit"] == 50
        return DataCenterPage(items=(), next_cursor=None)

    def list_resolution_runs(self, **filters: object) -> DataCenterPage:
        assert filters == {"conflicts_only": True, "cursor": None, "limit": 7}
        return DataCenterPage(
            items=({"run_id": "data_resolution:run", "status": "BLOCKED"},),
            next_cursor=None,
        )

    def get_resolution_run(self, run_id: str) -> dict[str, object]:
        return {"run_id": run_id, "default_decision": None}

    def resolution_decisions(self, **filters: object) -> DataCenterPage:
        return DataCenterPage(
            items=({"kind": "conflict" if filters["conflicts"] else "exception"},),
            next_cursor=None,
        )

    def get_version_lineage(self, version_id: str) -> dict[str, object]:
        return {
            "version": {"version_id": version_id, "status": "PUBLISHED"},
            "source_batches": [{"batch_id": "data_batch:batch", "status": "SUCCEEDED"}],
            "quality_runs": [{"run_id": "data_quality:quality", "status": "PASSED"}],
            "difference_reports": [{"report_id": "data_diff:report"}],
            "resolution": {"run_id": "data_resolution:resolution"},
        }

    def get_version_source_projection(self, version_id: str) -> dict[str, object]:
        return {
            "version_id": version_id,
            "release_manifest_sha256": "a" * 64,
            "sources": [
                {
                    "role": "A",
                    "upstream": "BAOSTOCK/BAOSTOCK",
                    "planned_endpoints": 4,
                    "actual_endpoints": 4,
                    "batches": 2,
                    "rows": 240,
                    "latest_status": "SUCCEEDED",
                    "latest_ended_at": None,
                },
                {
                    "role": "B",
                    "upstream": "AKSHARE/TENCENT",
                    "planned_endpoints": 4,
                    "actual_endpoints": 4,
                    "batches": 2,
                    "rows": 240,
                    "latest_status": "SUCCEEDED",
                    "latest_ended_at": None,
                },
                {
                    "role": "C",
                    "upstream": "AKSHARE/SINA",
                    "planned_endpoints": 4,
                    "actual_endpoints": 0,
                    "batches": 0,
                    "rows": 0,
                    "latest_status": None,
                    "latest_ended_at": None,
                },
            ],
            "legacy_disposition": {
                "source": "XTQUANT_LEGACY",
                "status": "REJECTED",
                "adr": "ADR-027",
                "included_batches": 0,
                "published_count": 0,
            },
        }

    def get_version_security_coverage(
        self, version_id: str, *, cursor: str | None, limit: int
    ) -> DataCenterPage:
        assert cursor is None
        assert limit == 25
        return DataCenterPage(
            items=(
                {
                    "code": "000001.SZ",
                    "segments": [{"start": "2026-06-11", "end": "2026-08-09"}],
                    "row_count": 60,
                },
                {
                    "code": "300750.SZ",
                    "segments": [
                        {"start": "2026-06-11", "end": "2026-06-30"},
                        {"start": "2026-07-04", "end": "2026-08-09"},
                    ],
                    "row_count": 57,
                },
            ),
            next_cursor=None,
        )


def test_data_center_routes_are_injected_without_source_readiness_api() -> None:
    app = create_app(_Jobs(), data_center_port=_DataCenter())
    with TestClient(app) as client:
        summary = client.get("/api/data/summary")
        sources = client.get("/api/data/sources")
        batches = client.get("/api/data/batches")

    assert summary.status_code == 200
    assert summary.json()["failures"] == 0
    assert sources.status_code == 404
    assert batches.status_code == 200
    assert batches.json() == {"items": [], "next_cursor": None}


def test_list_contract_rejects_invalid_cursor_limit_and_enum() -> None:
    app = create_app(_Jobs(), data_center_port=_DataCenter())
    with TestClient(app) as client:
        bad_cursor = client.get("/api/data/batches?cursor=not-a-cursor")
        bad_limit = client.get("/api/data/batches?limit=101")
        bad_dataset = client.get("/api/data/batches?dataset=unknown")

    assert bad_cursor.status_code == 400
    assert bad_limit.status_code == 422
    assert bad_dataset.status_code == 422


def test_resolution_routes_expose_bounded_list_detail_exceptions_and_conflicts() -> None:
    run_id = "data_resolution:00000000-0000-4000-8000-000000000006"
    app = create_app(_Jobs(), data_center_port=_DataCenter())
    with TestClient(app) as client:
        listing = client.get("/api/data/resolution-runs?conflicts_only=true&limit=7")
        detail = client.get(f"/api/data/resolution-runs/{run_id}")
        exceptions = client.get(f"/api/data/resolution-runs/{run_id}/exceptions?limit=3")
        conflicts = client.get(f"/api/data/resolution-runs/{run_id}/conflicts?limit=4")

    assert listing.status_code == 200
    assert listing.json()["items"][0]["status"] == "BLOCKED"
    assert detail.status_code == 200
    assert detail.json() == {"run_id": run_id, "default_decision": None}
    assert exceptions.status_code == 200
    assert exceptions.json()["items"] == [{"kind": "exception"}]
    assert conflicts.status_code == 200
    assert conflicts.json()["items"] == [{"kind": "conflict"}]


def test_explicit_version_lineage_route_preserves_safe_projection() -> None:
    version_id = "data_version:00000000-0000-4000-8000-000000000006"
    app = create_app(_Jobs(), data_center_port=_DataCenter())
    with TestClient(app) as client:
        response = client.get(f"/api/data/versions/{version_id}/lineage")

    assert response.status_code == 200
    assert response.json()["version"] == {
        "version_id": version_id,
        "status": "PUBLISHED",
    }
    serialized = response.text
    for forbidden in (
        "artifact_relative_path",
        "request_params",
        "provider_payload",
        "data_root",
        "E:\\secret",
    ):
        assert forbidden not in serialized


def test_version_sources_route_projects_roles_budgets_and_legacy_rejection() -> None:
    version_id = "data_version:00000000-0000-4000-8000-000000000006"
    app = create_app(_Jobs(), data_center_port=_DataCenter())
    with TestClient(app) as client:
        response = client.get(f"/api/data/versions/{version_id}/sources")

    assert response.status_code == 200
    body = response.json()
    assert [source["upstream"] for source in body["sources"]] == [
        "BAOSTOCK/BAOSTOCK",
        "AKSHARE/TENCENT",
        "AKSHARE/SINA",
    ]
    for source in body["sources"]:
        assert source["actual_endpoints"] <= source["planned_endpoints"]
    assert body["legacy_disposition"] == {
        "source": "XTQUANT_LEGACY",
        "status": "REJECTED",
        "adr": "ADR-027",
        "included_batches": 0,
        "published_count": 0,
    }
    serialized = response.text
    for forbidden in ("artifact_relative_path", "provider_payload", "data_root", "E:\\"):
        assert forbidden not in serialized


def test_version_security_coverage_route_pages_per_symbol_segments_and_gaps() -> None:
    version_id = "data_version:00000000-0000-4000-8000-000000000006"
    app = create_app(_Jobs(), data_center_port=_DataCenter())
    with TestClient(app) as client:
        coverage = client.get(f"/api/data/versions/{version_id}/security-coverage?limit=25")
        bad_cursor = client.get(
            f"/api/data/versions/{version_id}/security-coverage?cursor=not-a-cursor"
        )
        bad_limit = client.get(f"/api/data/versions/{version_id}/security-coverage?limit=101")

    assert coverage.status_code == 200
    items = coverage.json()["items"]
    assert [item["code"] for item in items] == ["000001.SZ", "300750.SZ"]
    assert items[0]["segments"] == [{"start": "2026-06-11", "end": "2026-08-09"}]
    assert items[1]["segments"] == [
        {"start": "2026-06-11", "end": "2026-06-30"},
        {"start": "2026-07-04", "end": "2026-08-09"},
    ]
    assert coverage.json()["next_cursor"] is None
    assert bad_cursor.status_code == 400
    assert bad_limit.status_code == 422


def test_read_lineage_corruption_maps_to_stable_sanitized_409() -> None:
    class _CorruptDataCenter(_DataCenter):
        def get_difference_report(self, report_id: str) -> object:
            del report_id
            raise DataCenterReadIntegrityError(
                r"SQLite E:\secret\warehouse payload token=do-not-leak"
            )

    app = create_app(_Jobs(), data_center_port=_CorruptDataCenter())
    with TestClient(app) as client:
        response = client.get(
            "/api/data/difference-reports/data_diff:00000000-0000-4000-8000-000000000006"
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "data_lineage_invalid",
            "message": "数据血缘验证失败。",
        }
    }
    assert "secret" not in response.text
    assert "token" not in response.text
