"""S1-06-R1 regression tests for strict read-side data-center contracts."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.center import (
    DataCenterQueryService,
    InvalidDataCenterQueryError,
)
from quantking.data.models import (
    DataProvider,
    DatasetKind,
    DataUpstream,
    IngestionBatch,
)
from quantking.jobs.models import JobStatus


class _Uow:
    def __init__(self, repository: object) -> None:
        self.data = repository

    def __enter__(self) -> _Uow:
        return self

    def __exit__(self, *args: object) -> None:
        del args


class _Jobs:
    def list_jobs_page(self, **filters: object) -> list[object]:
        del filters
        return []

    def count_jobs(self, **filters: object) -> int:
        del filters
        return 0


class _CountingJobs(_Jobs):
    def count_jobs(self, **filters: object) -> int:
        assert filters == {"job_type": "data.eod.v1", "status": JobStatus.FAILED}
        return 2


class _Preview:
    def query_by_id(self, **request: object) -> object:
        del request
        return SimpleNamespace(
            to_pylist=lambda: [
                {
                    "code": "000001.SZ",
                    "trade_date": datetime(2026, 7, 1).date(),
                    "close": 10.0,
                    "volume": 100,
                },
                {
                    "code": "000001.SZ",
                    "trade_date": datetime(2026, 7, 2).date(),
                    "close": 11.0,
                    "volume": 101,
                },
            ]
        )

    def verify_manifest_by_id(self, **request: object) -> object:
        del request
        raise AssertionError("manifest verification was not configured")


class _Verifier:
    def __init__(self, verified: object | None = None) -> None:
        self.verified = verified or SimpleNamespace(
            report_sha256="a" * 64,
            row_differences=[{"field": "close"}],
            created_at=UtcTimestamp(datetime(2026, 7, 1, tzinfo=UTC)),
        )

    def verify(self, **request: object) -> object:
        del request
        return self.verified


def _service(repository: object, *, verifier: object | None = None) -> DataCenterQueryService:
    return DataCenterQueryService(
        data_uow_factory=lambda: _Uow(repository),
        preview=_Preview(),
        difference_verifier=verifier or _Verifier(),
        jobs=_Jobs(),
    )


def _batch(index: int) -> IngestionBatch:
    return IngestionBatch.running(
        batch_id=new_entity_id("data_batch"),
        provider=DataProvider.AKSHARE,
        upstream=DataUpstream.EASTMONEY,
        endpoint="stock_zh_a_hist",
        library_version="test",
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        adapter_version="s1-06-r1-test",
        request_params={
            "provider": "AKSHARE",
            "upstream": "EASTMONEY",
            "endpoint": "stock_zh_a_hist",
            "akshare_version": "test",
            "index": str(index),
        },
        started_at=UtcTimestamp(datetime(2026, 7, index, tzinfo=UTC)),
    )


def _opaque_cursor(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_batch_cursor_cannot_be_reused_across_dataset_filters() -> None:
    rows = [_batch(3), _batch(2)]
    repository = SimpleNamespace(list_batches_page=lambda **filters: rows)
    service = _service(repository)

    first = service.list_batches(dataset="exchange_calendar", limit=1)
    assert first.next_cursor

    with pytest.raises(InvalidDataCenterQueryError, match="cursor"):
        service.list_batches(
            dataset="daily_bar_unadjusted",
            cursor=first.next_cursor,
            limit=1,
        )


def test_preview_cursor_cannot_be_reused_across_field_projection() -> None:
    version_id = str(new_entity_id("data_version"))
    version = SimpleNamespace(
        version_id=new_entity_id("data_version"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        created_at=UtcTimestamp(datetime(2026, 7, 1, tzinfo=UTC)),
    )
    repository = SimpleNamespace(get_version=lambda requested: version)
    service = _service(repository)

    first = service.preview(
        version_id=version_id,
        fields=("close",),
        codes=("000001.SZ",),
        start_date=None,
        end_date=None,
        cursor=None,
        limit=1,
    )
    assert first.next_cursor

    with pytest.raises(InvalidDataCenterQueryError, match="cursor"):
        service.preview(
            version_id=version_id,
            fields=("volume",),
            codes=("000001.SZ",),
            start_date=None,
            end_date=None,
            cursor=first.next_cursor,
            limit=1,
        )


def test_difference_rows_non_numeric_offset_is_a_stable_cursor_error() -> None:
    report_id = str(new_entity_id("data_diff"))
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "filters": {"report_id": report_id},
                "resource": "difference-rows",
                "sort_version": "created-desc-id-desc.v1",
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    cursor = _opaque_cursor(
        {
            "v": 2,
            "k": f"difference-rows:{report_id}",
            "q": fingerprint,
            "t": "2026-07-01T00:00:00Z",
            "i": report_id,
            "s": [],
            "o": "not-an-integer",
        }
    )
    service = _service(SimpleNamespace())

    with pytest.raises(InvalidDataCenterQueryError, match="cursor"):
        service.difference_rows(report_id=report_id, cursor=cursor, limit=50)


def test_summary_uses_aggregate_counts_without_loading_failure_pages() -> None:
    class _Repository:
        def count_failed_entities(self) -> int:
            return 7

        def list_versions_page(self, **filters: object) -> list[object]:
            del filters
            return []

        def list_batches_page(self, **filters: object) -> list[object]:
            raise AssertionError(f"summary loaded failed batches: {filters}")

        def list_quality_runs_page(self, **filters: object) -> list[object]:
            raise AssertionError(f"summary loaded failed quality runs: {filters}")

    service = DataCenterQueryService(
        data_uow_factory=lambda: _Uow(_Repository()),
        preview=_Preview(),
        difference_verifier=_Verifier(),
        jobs=_CountingJobs(),
    )

    assert service.summary()["failures"] == 9


def test_same_report_sha_cannot_mask_different_artifact_lineage() -> None:
    report_id = new_entity_id("data_diff")
    primary_version_id = new_entity_id("data_version")
    fallback_version_id = new_entity_id("data_version")
    primary_batch_id = new_entity_id("data_batch")
    fallback_batch_id = new_entity_id("data_batch")
    primary_quality_id = new_entity_id("data_quality")
    fallback_quality_id = new_entity_id("data_quality")
    created_at = UtcTimestamp(datetime(2026, 7, 1, tzinfo=UTC))
    report = SimpleNamespace(
        report_id=report_id,
        version_id=primary_version_id,
        primary_source=DataUpstream.EASTMONEY,
        fallback_source=DataUpstream.TENCENT,
        created_at=created_at,
        field_differences={"close": "1"},
        total_compared=1,
        total_differences=1,
        primary_batch_id=primary_batch_id,
        fallback_batch_id=fallback_batch_id,
        primary_version_id=primary_version_id,
        fallback_version_id=fallback_version_id,
        primary_content_sha256="1" * 64,
        fallback_content_sha256="2" * 64,
        primary_quality_run_id=primary_quality_id,
        fallback_quality_run_id=fallback_quality_id,
        sample_spec_sha256="3" * 64,
        threshold_rule_version="threshold.v1",
        report_sha256="a" * 64,
        report_relative_path=f"data-diff/{report_id}",
    )
    verified = SimpleNamespace(
        report_id=report_id,
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED.value,
        primary_source=DataUpstream.EASTMONEY.value,
        fallback_source=DataUpstream.TENCENT.value,
        created_at=created_at,
        field_differences={"close": "1"},
        total_compared=1,
        total_differences=1,
        primary_batch_id=new_entity_id("data_batch"),
        fallback_batch_id=fallback_batch_id,
        primary_version_id=new_entity_id("data_version"),
        fallback_version_id=fallback_version_id,
        primary_content_sha256="1" * 64,
        fallback_content_sha256="2" * 64,
        primary_quality_run_id=new_entity_id("data_quality"),
        fallback_quality_run_id=fallback_quality_id,
        sample_spec_sha256="3" * 64,
        threshold_rule_version="threshold.v1",
        report_sha256="a" * 64,
        report_relative_path=f"data-diff/{report_id}",
        row_differences=[],
    )
    repository = SimpleNamespace(get_difference_report=lambda requested: report)
    service = _service(repository, verifier=_Verifier(verified))

    with pytest.raises(InvalidDataCenterQueryError, match=r"credentials|lineage"):
        service.get_difference_report(str(report_id))
