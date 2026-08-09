from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, date, datetime

import pytest

from quantking.common.time import UtcTimestamp
from quantking.data.adapters.sources.akshare import AkshareDailyBarAdapter
from quantking.data.adapters.sources.contracts import (
    AmountUnit,
    SourceAdapterError,
    SourceErrorCode,
    VolumeUnit,
)
from quantking.data.adapters.sources.parameters import DailyBarRequest
from quantking.data.adapters.sources.registry import source_registry
from quantking.data.adapters.sources.units import AKSHARE_UNIT_POLICY_VERSION
from quantking.data.models import DataProvider, DatasetKind, DataUpstream


class FakeFrame:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.columns = tuple(rows[0]) if rows else ()
        self.empty = not rows

    def to_dict(self, orient: str) -> object:
        assert orient == "records"
        return self._rows


class FakeModule:
    def __init__(self, rows: list[dict[str, object]], *, version: str = "1.18.83") -> None:
        self.__version__ = version
        self.rows = rows
        self.calls: list[tuple[str, dict[str, str | float]]] = []

    def _call(self, endpoint: str, kwargs: dict[str, str | float]) -> FakeFrame:
        self.calls.append((endpoint, kwargs))
        return FakeFrame(self.rows)

    def stock_zh_a_hist(self, **kwargs: str | float) -> FakeFrame:
        return self._call("stock_zh_a_hist", kwargs)

    def stock_zh_a_hist_tx(self, **kwargs: str | float) -> FakeFrame:
        return self._call("stock_zh_a_hist_tx", kwargs)

    def stock_zh_a_daily(self, **kwargs: str | float) -> FakeFrame:
        return self._call("stock_zh_a_daily", kwargs)


def _clock() -> UtcTimestamp:
    return UtcTimestamp(datetime(2026, 8, 8, 1, 2, 3, tzinfo=UTC))


def _rows(upstream: DataUpstream) -> list[dict[str, object]]:
    if upstream is DataUpstream.EASTMONEY:
        return [
            {
                "日期": "2026-08-07",
                "开盘": 10.0,
                "最高": 10.5,
                "最低": 9.9,
                "收盘": 10.2,
                "昨收": 10.0,
                "成交量": 123,
                "成交额": 456.0,
            }
        ]
    row: dict[str, object] = {
        "date": date(2026, 8, 7),
        "open": 10.0,
        "high": 10.5,
        "low": 9.9,
        "close": 10.2,
        "pre_close": 10.0,
        "volume": 12_300,
        "amount": 456.0,
        "turnover": 0.1,
    }
    if upstream is DataUpstream.SINA:
        row["outstanding_share"] = 1_000_000.0
    return [row]


@pytest.mark.parametrize("upstream", tuple(DataUpstream))
def test_each_adapter_calls_only_its_fixed_endpoint_once(upstream: DataUpstream) -> None:
    module = FakeModule(_rows(upstream))
    adapter = AkshareDailyBarAdapter(upstream, module_loader=lambda: module, clock=_clock)

    result = adapter.fetch(DailyBarRequest("600000.SH", date(2026, 8, 1), date(2026, 8, 8)))

    spec = next(item for item in source_registry() if item.upstream is upstream)
    assert module.calls[0][0] == spec.endpoint
    assert len(module.calls) == 1
    assert module.calls[0][1]["adjust"] == ""
    assert module.calls[0][1]["start_date"] == "20260717"
    assert module.calls[0][1]["end_date"] == "20260808"
    if upstream is DataUpstream.SINA:
        assert "timeout" not in module.calls[0][1]
    else:
        assert module.calls[0][1]["timeout"] == 15.0
    assert result.lineage.provider is DataProvider.AKSHARE
    assert result.lineage.upstream is upstream
    assert result.lineage.endpoint == spec.endpoint
    assert result.lineage.library_version == "1.18.83"
    assert result.lineage.fetched_at == "2026-08-08T01:02:03Z"
    assert result.records[0]["stock_code"] == "600000.SH"
    assert result.records[0]["pre_close"] == 10.0
    assert result.complete is True
    expected_volume_unit = (
        VolumeUnit.LOT_100_SHARES if upstream is DataUpstream.EASTMONEY else VolumeUnit.SHARE
    )
    assert result.volume_unit is expected_volume_unit
    assert result.amount_unit is AmountUnit.CNY_YUAN
    assert result.unit_policy_version == AKSHARE_UNIT_POLICY_VERSION
    assert result.units_verified is True
    assert len(result.content_sha256) == 64


def test_context_seeds_target_pre_close_and_is_excluded_from_canonical() -> None:
    module = FakeModule(
        [
            {
                "date": date(2026, 8, 5),
                "open": 9.7,
                "high": 9.9,
                "low": 9.6,
                "close": 9.8,
                "volume": 10_000,
                "amount": 98_000.0,
            },
            {
                "date": date(2026, 8, 6),
                "open": 9.8,
                "high": 10.1,
                "low": 9.7,
                "close": 10.0,
                "volume": 11_000,
                "amount": 110_000.0,
            },
            {
                "date": date(2026, 8, 7),
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "close": 10.2,
                "volume": 12_300,
                "amount": 125_000.0,
            },
        ]
    )
    adapter = AkshareDailyBarAdapter(
        DataUpstream.SINA,
        module_loader=lambda: module,
        clock=_clock,
    )

    result = adapter.fetch(DailyBarRequest("600000.SH", date(2026, 8, 6), date(2026, 8, 8)))

    assert len(module.calls) == 1
    assert result.row_count == 3
    assert result.complete is True
    assert tuple(row["date"] for row in result.records) == ("2026-08-06", "2026-08-07")
    assert result.records[0]["pre_close"] == 9.8
    assert result.provider_payload[0]["date"] == "2026-08-05"
    assert json.loads(result.raw_payload)[0]["date"] == "2026-08-05"


def test_context_without_any_target_rows_is_complete_empty_target() -> None:
    module = FakeModule(
        [
            {
                "date": date(2026, 8, 5),
                "open": 9.7,
                "high": 9.9,
                "low": 9.6,
                "close": 9.8,
                "volume": 10_000,
                "amount": 98_000.0,
            }
        ]
    )
    adapter = AkshareDailyBarAdapter(
        DataUpstream.SINA,
        module_loader=lambda: module,
        clock=_clock,
    )

    result = adapter.fetch(DailyBarRequest("600000.SH", date(2026, 8, 6), date(2026, 8, 8)))

    assert result.row_count == 1
    assert result.records == ()
    assert result.complete is True
    assert result.partial_reason is None


def test_registry_has_only_three_daily_akshare_upstreams() -> None:
    registry = source_registry()
    assert tuple(item.upstream for item in registry) == tuple(DataUpstream)
    assert {item.provider for item in registry} == {DataProvider.AKSHARE}
    assert {item.dataset for item in registry} == {DatasetKind.DAILY_BAR_UNADJUSTED}
    assert {item.endpoint for item in registry} == {
        "stock_zh_a_hist",
        "stock_zh_a_hist_tx",
        "stock_zh_a_daily",
    }


def test_unverified_package_version_fails_before_endpoint_call() -> None:
    module = FakeModule(_rows(DataUpstream.EASTMONEY), version="1.18.84")
    adapter = AkshareDailyBarAdapter(
        DataUpstream.EASTMONEY,
        module_loader=lambda: module,
        clock=_clock,
    )

    with pytest.raises(SourceAdapterError) as caught:
        adapter.fetch(DailyBarRequest("600000.SH", date(2026, 8, 1), date(2026, 8, 8)))

    assert caught.value.code is SourceErrorCode.UNIT_UNVERIFIED
    assert module.calls == []


def test_empty_frame_preserves_lineage_for_explicit_ingestion_classification() -> None:
    empty = FakeModule([])
    adapter = AkshareDailyBarAdapter(DataUpstream.SINA, module_loader=lambda: empty, clock=_clock)
    request = DailyBarRequest("000001.SZ", date(2026, 8, 1), date(2026, 8, 8))
    result = adapter.fetch(request)

    assert len(empty.calls) == 1
    assert result.records == ()
    assert result.provider_payload == ()
    assert result.row_count == 0
    assert result.complete is True
    assert result.lineage.upstream is DataUpstream.SINA


def test_schema_drift_and_rate_limit_have_stable_errors() -> None:
    request = DailyBarRequest("000001.SZ", date(2026, 8, 1), date(2026, 8, 8))

    drift = FakeModule([{"date": "2026-08-07"}])
    adapter = AkshareDailyBarAdapter(DataUpstream.SINA, module_loader=lambda: drift, clock=_clock)
    with pytest.raises(SourceAdapterError) as caught:
        adapter.fetch(request)
    assert caught.value.code is SourceErrorCode.SCHEMA_DRIFT

    class Response:
        status_code = 429

    class LimitedModule(FakeModule):
        def stock_zh_a_daily(self, **kwargs: str | float) -> FakeFrame:
            del kwargs
            error = RuntimeError("unstable third-party detail")
            error.response = Response()  # type: ignore[attr-defined]
            raise error

    limited = LimitedModule(_rows(DataUpstream.SINA))
    adapter = AkshareDailyBarAdapter(DataUpstream.SINA, module_loader=lambda: limited, clock=_clock)
    with pytest.raises(SourceAdapterError) as caught:
        adapter.fetch(request)
    assert caught.value.code is SourceErrorCode.RATE_LIMITED
    assert "unstable" not in str(caught.value)


def test_missing_first_pre_close_is_explicit_partial_and_later_row_is_safely_derived() -> None:
    module = FakeModule(
        [
            {
                "date": date(2026, 8, 6),
                "open": 9.8,
                "high": 10.1,
                "low": 9.7,
                "close": 10.0,
                "volume": 100,
                "amount": 1_000.0,
            },
            {
                "date": date(2026, 8, 7),
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "close": 10.2,
                "volume": 123,
                "amount": 456.0,
            },
        ]
    )
    adapter = AkshareDailyBarAdapter(
        DataUpstream.SINA,
        module_loader=lambda: module,
        clock=_clock,
    )

    result = adapter.fetch(DailyBarRequest("600000.SH", date(2026, 8, 1), date(2026, 8, 8)))

    assert result.complete is False
    assert result.partial_reason == SourceErrorCode.PARTIAL_RESPONSE.value
    assert result.row_count == 2
    assert tuple(record["date"] for record in result.records) == ("2026-08-07",)
    assert result.records[0]["pre_close"] == 10.0


def test_import_is_offline_and_does_not_import_akshare() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import quantking.data.adapters.sources.akshare; "
                "assert 'akshare' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
