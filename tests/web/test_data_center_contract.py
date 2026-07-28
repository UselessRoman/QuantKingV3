from __future__ import annotations

import sys

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
            "blocked_sources": 23,
            "generated_at": "2026-07-28T00:00:00Z",
        }

    def sources(self) -> list[dict[str, object]]:
        return [
            {
                "source": "XTQUANT",
                "dataset": "exchange_calendar",
                "role": "PRIMARY",
                "status": "PRODUCTION_READY",
                "evidence_kind": "REAL",
                "blocking_reason": None,
                "auto_switch": False,
            }
        ]

    def list_batches(self, **filters: object) -> DataCenterPage:
        assert filters["limit"] == 50
        return DataCenterPage(items=(), next_cursor=None)


def test_data_center_routes_are_injected_and_do_not_load_xtquant() -> None:
    before = set(sys.modules)
    app = create_app(_Jobs(), data_center_port=_DataCenter())
    with TestClient(app) as client:
        summary = client.get("/api/data/summary")
        sources = client.get("/api/data/sources")
        batches = client.get("/api/data/batches")

    assert summary.status_code == 200
    assert summary.json()["blocked_sources"] == 23
    assert sources.status_code == 200
    assert sources.json()[0]["auto_switch"] is False
    assert batches.status_code == 200
    assert batches.json() == {"items": [], "next_cursor": None}
    assert "xtquant" not in {name.casefold() for name in set(sys.modules) - before}


def test_list_contract_rejects_invalid_cursor_limit_and_enum() -> None:
    app = create_app(_Jobs(), data_center_port=_DataCenter())
    with TestClient(app) as client:
        bad_cursor = client.get("/api/data/batches?cursor=not-a-cursor")
        bad_limit = client.get("/api/data/batches?limit=101")
        bad_dataset = client.get("/api/data/batches?dataset=unknown")

    assert bad_cursor.status_code == 400
    assert bad_limit.status_code == 422
    assert bad_dataset.status_code == 422


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
