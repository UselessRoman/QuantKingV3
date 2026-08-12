from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.persistence.sqlite import (
    DataSqliteUnitOfWorkFactory,
    create_data_engine,
)
from quantking.data.adapters.sources.baostock import (
    BaostockDailyBarAdapter,
    BaostockMarketDataGateway,
    BaostockSessionResult,
)
from quantking.data.adapters.sources.baostock_worker import BAOSTOCK_FIELDS
from quantking.data.adapters.sources.contracts import SupplierResponse
from quantking.data.adapters.sources.ingestion import MarketDataIngestionService
from quantking.data.adapters.warehouse.raw_store import RawBatchPublisher
from quantking.data.incremental import IncrementalPlanRequest, build_incremental_plan
from quantking.data.models import (
    BatchFailureKind,
    DataProvider,
    DatasetKind,
    DataUpstream,
    IngestionBatchStatus,
)
from quantking.jobs.adapters.persistence.sqlite import initialize_database

_NOW = UtcTimestamp(datetime(2026, 8, 12, 1, 2, 3, tzinfo=UTC))


def _raw_row(day: str, close: str, official_preclose: str) -> tuple[str, ...]:
    values = {
        "date": day,
        "code": "sh.600000",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "preclose": official_preclose,
        "volume": "100",
        "amount": "1000.50",
        "adjustflag": "3",
        "tradestatus": "1",
        "isST": "0",
    }
    return tuple(values[field] for field in BAOSTOCK_FIELDS)


class _Runner:
    def __init__(self, rows: tuple[tuple[str, ...], ...]) -> None:
        self.rows = rows

    def run(self, _request: object) -> BaostockSessionResult:
        return BaostockSessionResult(
            rows=self.rows,
            distribution_version="0.9.3",
            runtime_version="00.9.30",
            login_messages=1,
            query_messages=1,
            logout_messages=1,
            complete=True,
            partial_reason=None,
        )


def _plan():
    return build_incremental_plan(
        IncrementalPlanRequest(
            batch_id=new_entity_id("data_batch"),
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            scope=("600000.SH",),
            request_start=TradingDate(date(2026, 8, 1)),
            request_end=TradingDate(date(2026, 8, 8)),
            start_inclusive=True,
            end_inclusive=True,
            known_coverage=(),
            lookback_days=0,
            max_days_per_chunk=31,
            max_items_per_chunk=1,
            adapter_version="baostock-daily-bar.v1",
        )
    )


def _service(tmp_path: Path, rows: tuple[tuple[str, ...], ...]) -> MarketDataIngestionService:
    database = tmp_path / "data.db"
    initialize_database(database)
    factory = DataSqliteUnitOfWorkFactory(create_data_engine(database, busy_timeout_ms=250))
    adapter = BaostockDailyBarAdapter(
        runner=_Runner(rows),  # type: ignore[arg-type]
        clock=lambda: _NOW,
    )
    return MarketDataIngestionService(
        gateway=BaostockMarketDataGateway(adapter),
        raw_publisher=RawBatchPublisher(data_root=tmp_path),
        data_root=tmp_path,
        uow_factory=factory,
        clock=lambda: _NOW,
    )


def test_baostock_success_round_trips_raw_and_persists_distinct_lineage(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        (
            _raw_row("2026-07-31", "10.00", "9.90"),
            _raw_row("2026-08-07", "10.50", "99.99"),
        ),
    )
    plan = _plan()
    outcome = service.run(plan, code_version="s1-07k3")

    assert outcome.batch.status is IngestionBatchStatus.SUCCEEDED
    assert outcome.batch.provider is DataProvider.BAOSTOCK
    assert outcome.batch.upstream is DataUpstream.BAOSTOCK
    assert outcome.batch.endpoint == "query_history_k_data_plus"
    assert outcome.batch.library_version == "0.9.3"
    assert outcome.canonical_table is not None
    assert outcome.canonical_table.to_pylist()[0]["pre_close"] == 10.0
    assert outcome.raw_directory is not None
    manifest = json.loads((outcome.raw_directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["request_params"]["provider"] == "BAOSTOCK"
    assert manifest["request_params"]["upstream"] == "BAOSTOCK"
    assert manifest["request_params"]["baostock_version"] == "0.9.3"
    assert "akshare_version" not in manifest["request_params"]
    raw_path = outcome.raw_directory / "responses" / "chunk-00000.json"
    rebuilt = SupplierResponse.from_raw_payload(
        raw_path.read_bytes(), expected_chunk=plan.chunks[0]
    )
    assert rebuilt.contract.lineage.provider is DataProvider.BAOSTOCK
    assert rebuilt.records == (
        {
            "amount": 1000.5,
            "close": 10.5,
            "date": "2026-08-07",
            "high": 10.5,
            "low": 10.5,
            "open": 10.5,
            "pre_close": 10.0,
            "stock_code": "600000.SH",
            "volume": 100,
        },
    )


def test_baostock_missing_context_lands_partial_raw_without_canonical(tmp_path: Path) -> None:
    service = _service(tmp_path, (_raw_row("2026-08-07", "10.50", "99.99"),))
    plan = _plan()
    outcome = service.run(plan, code_version="s1-07k3")

    assert outcome.batch.status is IngestionBatchStatus.PARTIAL
    assert outcome.batch.failure is not None
    assert outcome.batch.failure.error_type is BatchFailureKind.PARTIAL_SOURCE
    assert outcome.batch.failure.message == "BaoStock returned a partial response"
    assert outcome.canonical_table is None
    assert outcome.raw_directory is not None
    response = SupplierResponse.from_raw_payload(
        (outcome.raw_directory / "responses" / "chunk-00000.json").read_bytes(),
        expected_chunk=plan.chunks[0],
    )
    assert response.complete is False
    assert response.partial_reason == "BAOSTOCK_PARTIAL_RESPONSE"
    raw = json.loads(response.raw_payload)
    assert raw["provider_payload"]["response_metadata"]["query_messages"] == 1
