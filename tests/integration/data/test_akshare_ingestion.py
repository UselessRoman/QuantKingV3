"""Offline AKShare ingestion integration at the K1 Raw/Canonical boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.data.adapters.test_akshare_adapter import FakeMarketDataGateway

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.sources.contracts import SourceAdapterError, SourceErrorCode
from quantking.data.adapters.sources.ingestion import AkshareIngestionService
from quantking.data.adapters.warehouse.raw_store import RawBatchPublisher
from quantking.data.incremental import IncrementalPlanRequest, build_incremental_plan
from quantking.data.models import (
    BatchFailureKind,
    DataProvider,
    DatasetKind,
    DataUpstream,
    EmptyResultInfo,
    IngestionBatchStatus,
    LegalEmptyClassification,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database

_NOW = UtcTimestamp(datetime(2026, 8, 8, 1, 2, 3, tzinfo=UTC))

_SUCCESS_RECORDS = [
    {
        "stock_code": "600000.SH",
        "date": "2026-08-07",
        "open": 10.0,
        "high": 10.5,
        "low": 9.9,
        "close": 10.2,
        "pre_close": 10.0,
        "volume": 12_300,
        "amount": 456.0,
    }
]


def _plan(
    dataset: DatasetKind = DatasetKind.DAILY_BAR_UNADJUSTED,
    *,
    request_start: date = date(2026, 8, 1),
    request_end: date = date(2026, 8, 8),
):
    return build_incremental_plan(
        IncrementalPlanRequest(
            batch_id=new_entity_id("data_batch"),
            dataset=dataset,
            scope=("SH",) if dataset is DatasetKind.EXCHANGE_CALENDAR else ("600000.SH",),
            request_start=TradingDate(request_start),
            request_end=TradingDate(request_end),
            start_inclusive=True,
            end_inclusive=True,
            known_coverage=(),
            lookback_days=0,
            max_days_per_chunk=31,
            max_items_per_chunk=1,
            adapter_version="akshare-daily-bar.v2",
        )
    )


def _service(
    tmp_path: Path,
    gateway: FakeMarketDataGateway,
) -> tuple[AkshareIngestionService, DataSqliteUnitOfWorkFactory]:
    database = tmp_path / "data.db"
    initialize_database(database)
    engine = create_data_engine(database, busy_timeout_ms=250)
    factory = DataSqliteUnitOfWorkFactory(engine)
    return (
        AkshareIngestionService(
            gateway=gateway,
            raw_publisher=RawBatchPublisher(data_root=tmp_path),
            data_root=tmp_path,
            uow_factory=factory,
            clock=lambda: _NOW,
        ),
        factory,
    )


def test_success_persists_provider_upstream_endpoint_and_raw_lineage(tmp_path: Path) -> None:
    gateway = FakeMarketDataGateway(upstream=DataUpstream.TENCENT, records=_SUCCESS_RECORDS)
    service, factory = _service(tmp_path, gateway)
    plan = _plan()

    outcome = service.run(plan)

    assert outcome.batch.status is IngestionBatchStatus.SUCCEEDED
    assert outcome.batch.provider is DataProvider.AKSHARE
    assert outcome.batch.upstream is DataUpstream.TENCENT
    assert outcome.batch.endpoint == "stock_zh_a_hist_tx"
    assert outcome.batch.library_version == "1.18.83"
    assert outcome.canonical_table is not None
    assert outcome.canonical_table.to_pylist() == [
        {
            "code": "600000.SH",
            "trade_date": date(2026, 8, 7),
            "open": 10.0,
            "high": 10.5,
            "low": 9.9,
            "close": 10.2,
            "pre_close": 10.0,
            "volume": 12_300,
            "amount": 456.0,
        }
    ]
    assert outcome.raw_directory is not None
    manifest = json.loads((outcome.raw_directory / "manifest.json").read_text(encoding="utf-8"))
    params = manifest["request_params"]
    assert params["provider"] == "AKSHARE"
    assert params["upstream"] == "TENCENT"
    assert params["endpoint"] == "stock_zh_a_hist_tx"
    assert params["akshare_version"] == "1.18.83"
    assert params["fetched_at"] == "2026-08-08T01:02:03Z"
    assert params["request_start"] == "2026-08-01"
    assert params["request_end"] == "2026-08-08"
    assert params["target_start"] == "2026-08-01"
    assert params["target_end"] == "2026-08-08"
    assert params["upstream_start"] == "2026-07-17"
    assert params["upstream_end"] == "2026-08-08"
    assert params["lookback_days"] == "15"
    assert params["row_count"] == "1"
    assert params["upstream_row_count"] == "1"
    assert params["canonical_row_count"] == "1"
    assert manifest["files"][0]["row_count"] == 1
    raw_file = outcome.raw_directory / "responses" / "chunk-00000.json"
    assert params["content_sha256"] == hashlib.sha256(raw_file.read_bytes()).hexdigest()
    raw_envelope = json.loads(raw_file.read_text(encoding="utf-8"))
    assert raw_envelope["request_window"] == {
        "lookback_days": 15,
        "target_end": "2026-08-08",
        "target_start": "2026-08-01",
        "upstream_end": "2026-08-08",
        "upstream_start": "2026-07-17",
    }
    with factory() as uow:
        assert uow.data.get_batch(str(plan.batch_id)) == outcome.batch


def test_empty_upstream_requires_explicit_non_trading_session(tmp_path: Path) -> None:
    gateway = FakeMarketDataGateway(upstream=DataUpstream.SINA, records=[])
    service, _ = _service(tmp_path, gateway)

    outcome = service.run(_plan())

    assert outcome.batch.status is IngestionBatchStatus.FAILED
    assert outcome.batch.failure is not None
    assert outcome.batch.failure.error_type is BatchFailureKind.UNEXPECTED_EMPTY
    assert outcome.raw_directory is not None
    manifest = json.loads((outcome.raw_directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["request_params"]["row_count"] == "0"
    assert manifest["request_params"]["upstream_row_count"] == "0"
    assert manifest["request_params"]["canonical_row_count"] == "0"


def test_explicit_non_trading_session_produces_legal_zero_row_raw_and_canonical(
    tmp_path: Path,
) -> None:
    gateway = FakeMarketDataGateway(upstream=DataUpstream.SINA, records=[])
    service, _ = _service(tmp_path, gateway)
    empty_result = EmptyResultInfo(
        LegalEmptyClassification.NO_TRADING_SESSION,
        "caller-verified exchange calendar has no trading session",
    )

    outcome = service.run(_plan(), empty_result=empty_result)

    assert outcome.batch.status is IngestionBatchStatus.SUCCEEDED
    assert outcome.batch.empty_result == empty_result
    assert outcome.canonical_table is not None
    assert outcome.canonical_table.num_rows == 0
    assert outcome.raw_directory is not None


def test_partial_pre_close_keeps_upstream_and_canonical_row_counts_distinct(
    tmp_path: Path,
) -> None:
    records = [
        {
            "stock_code": "600000.SH",
            "date": "2026-08-07",
            "open": 10.0,
            "high": 10.5,
            "low": 9.9,
            "close": 10.2,
            "pre_close": 10.0,
            "volume": 456,
            "amount": 456.0,
        }
    ]
    gateway = FakeMarketDataGateway(
        upstream=DataUpstream.TENCENT,
        records=records,
        row_count=2,
        complete=False,
        partial_reason=SourceErrorCode.PARTIAL_RESPONSE.value,
    )
    service, _ = _service(tmp_path, gateway)

    outcome = service.run(_plan())

    assert outcome.batch.status is IngestionBatchStatus.FAILED
    assert outcome.batch.row_count == 1
    assert outcome.raw_directory is not None
    manifest = json.loads((outcome.raw_directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["request_params"]["row_count"] == "2"
    assert manifest["request_params"]["upstream_row_count"] == "2"
    assert manifest["request_params"]["canonical_row_count"] == "1"
    assert manifest["files"][0]["row_count"] == 2


def test_context_is_raw_only_and_seeds_complete_target_manifest(tmp_path: Path) -> None:
    records = [
        {
            "stock_code": "600000.SH",
            "date": "2026-08-06",
            "open": 9.8,
            "high": 10.1,
            "low": 9.7,
            "close": 10.0,
            "pre_close": 9.8,
            "volume": 11_000,
            "amount": 110_000.0,
        },
        {
            "stock_code": "600000.SH",
            "date": "2026-08-07",
            "open": 10.0,
            "high": 10.5,
            "low": 9.9,
            "close": 10.2,
            "pre_close": 10.0,
            "volume": 12_300,
            "amount": 125_000.0,
        },
    ]
    gateway = FakeMarketDataGateway(upstream=DataUpstream.SINA, records=records, row_count=3)
    service, _ = _service(tmp_path, gateway)

    outcome = service.run(_plan(request_start=date(2026, 8, 6), request_end=date(2026, 8, 8)))

    assert outcome.batch.status is IngestionBatchStatus.SUCCEEDED
    assert outcome.canonical_table is not None
    assert tuple(outcome.canonical_table.column("trade_date").to_pylist()) == (
        date(2026, 8, 6),
        date(2026, 8, 7),
    )
    assert outcome.canonical_table.to_pylist()[0]["pre_close"] == 9.8
    assert outcome.raw_directory is not None
    manifest = json.loads((outcome.raw_directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["request_params"]["upstream_row_count"] == "3"
    assert manifest["request_params"]["canonical_row_count"] == "2"
    raw_envelope = json.loads(
        (outcome.raw_directory / "responses" / "chunk-00000.json").read_text(encoding="utf-8")
    )
    assert raw_envelope["response"]["upstream_row_count"] == 3
    assert len(raw_envelope["response"]["records"]) == 2
    assert raw_envelope["response"]["records"][0]["date"] == "2026-08-06"


def test_non_daily_is_rejected_before_import_or_endpoint_call(tmp_path: Path) -> None:
    gateway = FakeMarketDataGateway(upstream=DataUpstream.TENCENT, records=[])
    service, _ = _service(tmp_path, gateway)

    with pytest.raises(SourceAdapterError) as caught:
        service.run(_plan(DatasetKind.EXCHANGE_CALENDAR))

    assert caught.value.code is SourceErrorCode.REQUEST_REJECTED
