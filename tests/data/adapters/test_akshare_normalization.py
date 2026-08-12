from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from tests.data.adapters.test_akshare_adapter import FakeModule, _rows

from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
)
from quantking.data.adapters.sources.contracts import (
    SourceAdapterError,
    SourceErrorCode,
    SupplierResponse,
)
from quantking.data.adapters.sources.registry import AKSHARE_RUNTIME_UPSTREAMS
from quantking.data.incremental import FetchChunk
from quantking.data.models import (
    DatasetKind,
    DataUpstream,
    EmptyResultInfo,
    LegalEmptyClassification,
)


def _clock() -> UtcTimestamp:
    return UtcTimestamp(datetime(2026, 8, 8, tzinfo=UTC))


def _chunk(
    dataset: DatasetKind = DatasetKind.DAILY_BAR_UNADJUSTED,
    *,
    symbol: str = "600000.SH",
) -> FetchChunk:
    return FetchChunk(
        sequence=0,
        dataset=dataset,
        scope=("SH",) if dataset is DatasetKind.EXCHANGE_CALENDAR else (symbol,),
        start=TradingDate(date(2026, 8, 1)),
        end=TradingDate(date(2026, 8, 8)),
        request_sha256="a" * 64,
    )


@pytest.mark.parametrize("upstream", AKSHARE_RUNTIME_UPSTREAMS)
def test_gateway_maps_all_upstreams_to_same_sorted_canonical_schema(
    upstream: DataUpstream,
) -> None:
    module = FakeModule(_rows(upstream))
    gateway = AkshareMarketDataGateway(
        AkshareDailyBarAdapter(
            upstream,
            module_loader=lambda: module,
            clock=_clock,
        )
    )

    response = gateway.fetch(_chunk())
    from quantking.data.adapters.sources.normalization import normalize_akshare_response

    table = normalize_akshare_response(DatasetKind.DAILY_BAR_UNADJUSTED, response)
    assert table.column_names == [
        "code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "volume",
        "amount",
    ]
    assert table.to_pylist() == [
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


def test_tencent_000_sz_raw_lots_are_unchanged_until_canonical_normalization() -> None:
    rows = _rows(DataUpstream.TENCENT)
    rows[0]["volume"] = 123
    module = FakeModule(rows)
    gateway = AkshareMarketDataGateway(
        AkshareDailyBarAdapter(
            DataUpstream.TENCENT,
            module_loader=lambda: module,
            clock=_clock,
        )
    )

    response = gateway.fetch(_chunk(symbol="000001.SZ"))
    from quantking.data.adapters.sources.normalization import normalize_akshare_response

    assert response.contract.volume_unit.value == "lot_100_shares"
    raw_envelope = json.loads(response.raw_payload)
    provider_records = raw_envelope["provider_payload"]["records"]
    assert isinstance(provider_records, list)
    assert provider_records[0]["volume"] == 123
    table = normalize_akshare_response(DatasetKind.DAILY_BAR_UNADJUSTED, response)
    assert table.to_pylist()[0]["volume"] == 12_300
    assert table.to_pylist()[0]["code"] == "000001.SZ"


@pytest.mark.parametrize(
    ("upstream", "raw_volume", "volume_column"),
    (
        (DataUpstream.EASTMONEY, 123, "成交量"),
        (DataUpstream.TENCENT, 123, "volume"),
        (DataUpstream.SINA, 12_300, "volume"),
    ),
)
def test_000_sz_all_upstreams_normalize_to_shares_without_mutating_raw(
    upstream: DataUpstream,
    raw_volume: int,
    volume_column: str,
) -> None:
    rows = _rows(upstream)
    rows[0][volume_column] = raw_volume
    module = FakeModule(rows)
    gateway = AkshareMarketDataGateway(
        AkshareDailyBarAdapter(upstream, module_loader=lambda: module, clock=_clock)
    )

    response = gateway.fetch(_chunk(symbol="000001.SZ"))
    from quantking.data.adapters.sources.normalization import normalize_akshare_response

    raw_envelope = json.loads(response.raw_payload)
    assert raw_envelope["provider_payload"]["records"][0][volume_column] == raw_volume
    assert raw_envelope["provider_payload"]["request"] == {
        "lookback_days": 15,
        "symbol": "000001.SZ",
        "target_end": "2026-08-08",
        "target_start": "2026-08-01",
        "upstream_end": "2026-08-08",
        "upstream_start": "2026-07-17",
    }
    table = normalize_akshare_response(DatasetKind.DAILY_BAR_UNADJUSTED, response)
    assert table.to_pylist()[0]["volume"] == 12_300


def test_empty_target_with_context_still_requires_explicit_empty_classification() -> None:
    module = FakeModule(
        [
            {
                "date": date(2026, 7, 31),
                "open": 9.7,
                "high": 9.9,
                "low": 9.6,
                "close": 9.8,
                "volume": 10_000,
                "amount": 98_000.0,
            }
        ]
    )
    gateway = AkshareMarketDataGateway(
        AkshareDailyBarAdapter(
            DataUpstream.SINA,
            module_loader=lambda: module,
            clock=_clock,
        )
    )
    response = gateway.fetch(_chunk())
    from quantking.data.adapters.sources.normalization import normalize_akshare_response

    assert response.upstream_row_count == 1
    assert response.records == ()
    assert response.complete is True
    with pytest.raises(SourceAdapterError) as caught:
        normalize_akshare_response(DatasetKind.DAILY_BAR_UNADJUSTED, response)
    assert caught.value.code is SourceErrorCode.UNEXPECTED_EMPTY

    table = normalize_akshare_response(
        DatasetKind.DAILY_BAR_UNADJUSTED,
        response,
        empty_result=EmptyResultInfo(
            LegalEmptyClassification.NO_TRADING_SESSION,
            "caller-verified target window has no trading session",
        ),
    )
    assert table.num_rows == 0


def test_raw_rebuild_rejects_tampered_request_window_binding() -> None:
    module = FakeModule(_rows(DataUpstream.EASTMONEY))
    gateway = AkshareMarketDataGateway(
        AkshareDailyBarAdapter(
            DataUpstream.EASTMONEY,
            module_loader=lambda: module,
            clock=_clock,
        )
    )
    chunk = _chunk()
    response = gateway.fetch(chunk)
    envelope = json.loads(response.raw_payload)
    envelope["request_window"]["lookback_days"] = 14
    tampered = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()

    with pytest.raises(SourceAdapterError) as caught:
        SupplierResponse.from_raw_payload(tampered, expected_chunk=chunk)
    assert caught.value.code is SourceErrorCode.PARSE


def test_gateway_rejects_non_daily_dataset_before_endpoint_call() -> None:
    module = FakeModule(_rows(DataUpstream.EASTMONEY))
    gateway = AkshareMarketDataGateway(
        AkshareDailyBarAdapter(DataUpstream.EASTMONEY, module_loader=lambda: module, clock=_clock)
    )
    with pytest.raises(SourceAdapterError) as caught:
        gateway.fetch(_chunk(DatasetKind.EXCHANGE_CALENDAR))
    assert caught.value.code is SourceErrorCode.REQUEST_REJECTED
    assert module.calls == []


def test_unverified_units_block_canonical_normalization() -> None:
    module = FakeModule(_rows(DataUpstream.EASTMONEY), version="1.18.84")
    gateway = AkshareMarketDataGateway(
        AkshareDailyBarAdapter(DataUpstream.EASTMONEY, module_loader=lambda: module, clock=_clock)
    )
    with pytest.raises(SourceAdapterError) as caught:
        gateway.fetch(_chunk())
    assert caught.value.code is SourceErrorCode.UNIT_UNVERIFIED
    assert module.calls == []
