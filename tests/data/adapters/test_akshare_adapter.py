from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, date, datetime
from typing import Any

import pytest

from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.sources.akshare import (
    AKSHARE_UNIT_POLICY_VERSION,
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
    AkshareSessionEnvelope,
    AkshareSessionRunner,
)
from quantking.data.adapters.sources.akshare_worker import run_request
from quantking.data.adapters.sources.contracts import (
    AmountUnit,
    SourceAdapterError,
    SourceErrorCode,
    SourceLineage,
    SupplierResponse,
    SupplierResponseContract,
    VolumeUnit,
)
from quantking.data.adapters.sources.parameters import DailyBarRequest
from quantking.data.adapters.sources.registry import AKSHARE_RUNTIME_UPSTREAMS, source_registry
from quantking.data.adapters.sources.request_window import fixed_akshare_request_window
from quantking.data.incremental import FetchChunk
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

    def stock_zh_a_hist_tx(self, **kwargs: str | float) -> FakeFrame:
        return self._call("stock_zh_a_hist_tx", kwargs)

    def stock_zh_a_daily(self, **kwargs: str | float) -> FakeFrame:
        return self._call("stock_zh_a_daily", kwargs)


def _clock() -> UtcTimestamp:
    return UtcTimestamp(datetime(2026, 8, 8, 1, 2, 3, tzinfo=UTC))


def _rows(upstream: DataUpstream) -> list[dict[str, object]]:
    context: dict[str, object] = {
        "date": date(2026, 7, 31),
        "open": 9.4,
        "high": 9.6,
        "low": 9.3,
        "close": 9.5,
        "pre_close": 9.4,  # vendor reference value must never become Canonical
        "volume": 10_000,
        "amount": 95_000.0,
        "turnover": 0.1,
    }
    if upstream is DataUpstream.SINA:
        context["outstanding_share"] = 1_000_000.0
    row: dict[str, object] = {
        "date": date(2026, 8, 7),
        "open": 10.0,
        "high": 10.5,
        "low": 9.9,
        "close": 10.2,
        "pre_close": 99.9,  # vendor reference value must never become Canonical
        "volume": 12_300,
        "amount": 456.0,
        "turnover": 0.1,
    }
    if upstream is DataUpstream.SINA:
        row["outstanding_share"] = 1_000_000.0
    return [context, row]


class FakeRunner:
    """Run the real in-process worker core with a FakeModule loader."""

    def __init__(self, module: FakeModule, *, library_version: str | None = None) -> None:
        self._module = module
        self._library_version = library_version

    def run(self, request: DailyBarRequest, upstream: DataUpstream) -> AkshareSessionEnvelope:
        envelope = run_request(
            {
                "upstream": upstream.value,
                "symbol": request.symbol,
                "start_date": request.start_date.isoformat(),
                "end_date": request.end_date.isoformat(),
            },
            module_loader=lambda: self._module,
        )
        if not envelope["ok"]:
            stable = SourceErrorCode(str(envelope["error_code"]))
            raise SourceAdapterError(stable)
        return AkshareSessionEnvelope(
            rows=tuple({str(key): item for key, item in row.items()} for row in envelope["rows"]),
            library_version=str(self._library_version or envelope["library_version"]),
            upstream_row_count=int(envelope["row_count"]),
        )


class FakeMarketDataGateway:
    """Provider-neutral gateway fixture for ingestion/worker tests (no subprocess)."""

    def __init__(
        self,
        *,
        upstream: DataUpstream,
        records: list[dict[str, object]],
        row_count: int | None = None,
        complete: bool = True,
        partial_reason: str | None = None,
    ) -> None:
        self._upstream = upstream
        self._records = records
        self._row_count = row_count if row_count is not None else len(records)
        self._complete = complete
        self._partial_reason = partial_reason

    @property
    def provider(self) -> DataProvider:
        return DataProvider.AKSHARE

    @property
    def upstream(self) -> DataUpstream:
        return self._upstream

    @property
    def endpoint(self) -> str:
        return source_registry_dict()[self._upstream]

    def library_version(self) -> str:
        return "1.18.83"

    def fetch(self, chunk: FetchChunk) -> SupplierResponse:
        window = fixed_akshare_request_window(chunk.start.value, chunk.end.value)
        return SupplierResponse.create_bound(
            chunk=chunk,
            records=tuple(self._records),
            contract=SupplierResponseContract(
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                contract_version="akshare-daily-bar.v2",
                required_fields=(
                    "stock_code",
                    "date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "pre_close",
                    "volume",
                    "amount",
                ),
                volume_unit=VolumeUnit.SHARE,
                amount_unit=AmountUnit.CNY_YUAN,
                unadjusted=True,
                lineage=SourceLineage(
                    provider=self.provider,
                    upstream=self._upstream,
                    endpoint=self.endpoint,
                    library_version=self.library_version(),
                    fetched_at="2026-08-08T01:02:03Z",
                ),
            ),
            complete=self._complete,
            partial_reason=self._partial_reason,
            provider_payload={"records": list(self._records), "row_count": self._row_count},
            request_window=window,
            upstream_row_count=self._row_count,
        )


def source_registry_dict() -> dict[DataUpstream, str]:
    return {item.upstream: item.endpoint for item in source_registry()}


@pytest.mark.parametrize("upstream", AKSHARE_RUNTIME_UPSTREAMS)
def test_each_adapter_calls_only_its_fixed_endpoint_once(upstream: DataUpstream) -> None:
    module = FakeModule(_rows(upstream))
    adapter = AkshareDailyBarAdapter(
        upstream,
        runner=FakeRunner(module),
        module_loader=lambda: module,
        clock=_clock,
    )

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
    assert result.records[0]["pre_close"] == 9.5  # derived from the 07-31 close
    assert result.complete is True
    assert result.volume_unit is VolumeUnit.SHARE
    assert result.amount_unit is AmountUnit.CNY_YUAN
    assert result.unit_policy_version == AKSHARE_UNIT_POLICY_VERSION
    assert result.units_verified is True
    assert len(result.content_sha256) == 64


@pytest.mark.parametrize("upstream", AKSHARE_RUNTIME_UPSTREAMS)
def test_vendor_pre_close_column_never_becomes_canonical(upstream: DataUpstream) -> None:
    """K4B: Canonical pre_close derives only from the prior unadjusted close."""
    module = FakeModule(
        [
            {
                "date": date(2026, 8, 5),
                "open": 9.7,
                "high": 9.9,
                "low": 9.6,
                "close": 9.8,
                "pre_close": 999.0,
                "volume": 10_000,
                "amount": 98_000.0,
            },
            {
                "date": date(2026, 8, 6),
                "open": 9.8,
                "high": 10.1,
                "low": 9.7,
                "close": 10.0,
                "pre_close": 888.0,
                "volume": 11_000,
                "amount": 110_000.0,
            },
            {
                "date": date(2026, 8, 7),
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "close": 10.2,
                "pre_close": 777.0,
                "volume": 12_300,
                "amount": 125_000.0,
            },
        ]
    )
    adapter = AkshareDailyBarAdapter(
        upstream,
        runner=FakeRunner(module),
        module_loader=lambda: module,
        clock=_clock,
    )

    result = adapter.fetch(DailyBarRequest("600000.SH", date(2026, 8, 6), date(2026, 8, 8)))

    assert tuple(record["pre_close"] for record in result.records) == (9.8, 10.0)
    assert tuple(row["pre_close"] for row in result.provider_payload) == (999.0, 888.0, 777.0)


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
        runner=FakeRunner(module),
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
        runner=FakeRunner(module),
        module_loader=lambda: module,
        clock=_clock,
    )

    result = adapter.fetch(DailyBarRequest("600000.SH", date(2026, 8, 6), date(2026, 8, 8)))

    assert result.row_count == 1
    assert result.records == ()
    assert result.complete is True
    assert result.partial_reason is None


def test_registry_has_only_the_two_runtime_akshare_upstreams() -> None:
    registry = source_registry()
    assert tuple(item.upstream for item in registry) == AKSHARE_RUNTIME_UPSTREAMS
    assert {item.provider for item in registry} == {DataProvider.AKSHARE}
    assert {item.dataset for item in registry} == {DatasetKind.DAILY_BAR_UNADJUSTED}
    assert {item.endpoint for item in registry} == {"stock_zh_a_hist_tx", "stock_zh_a_daily"}
    from quantking.data.adapters.sources.registry import source_spec

    with pytest.raises(ValueError):
        source_spec(DataUpstream.EASTMONEY)


def test_unverified_package_version_fails_before_endpoint_call() -> None:
    module = FakeModule(_rows(DataUpstream.TENCENT), version="1.18.84")
    adapter = AkshareDailyBarAdapter(
        DataUpstream.TENCENT,
        runner=FakeRunner(module),
        module_loader=lambda: module,
        clock=_clock,
    )

    with pytest.raises(SourceAdapterError) as caught:
        adapter.fetch(DailyBarRequest("600000.SH", date(2026, 8, 1), date(2026, 8, 8)))

    assert caught.value.code is SourceErrorCode.UNIT_UNVERIFIED
    assert module.calls == []


def test_empty_frame_preserves_lineage_for_explicit_ingestion_classification() -> None:
    empty = FakeModule([])
    adapter = AkshareDailyBarAdapter(
        DataUpstream.SINA,
        runner=FakeRunner(empty),
        module_loader=lambda: empty,
        clock=_clock,
    )
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
    adapter = AkshareDailyBarAdapter(
        DataUpstream.SINA,
        runner=FakeRunner(drift),
        module_loader=lambda: drift,
        clock=_clock,
    )
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
    adapter = AkshareDailyBarAdapter(
        DataUpstream.SINA,
        runner=FakeRunner(limited),
        module_loader=lambda: limited,
        clock=_clock,
    )
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
        runner=FakeRunner(module),
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


def test_session_runner_hard_timeout_raises_stable_network_error() -> None:
    runner = AkshareSessionRunner(
        timeout_seconds=0.5,
        command=(sys.executable, "-c", "import time; time.sleep(5)"),
    )
    request = DailyBarRequest("600000.SH", date(2026, 8, 1), date(2026, 8, 8))
    with pytest.raises(SourceAdapterError) as caught:
        runner.run(request, DataUpstream.TENCENT)
    assert caught.value.code is SourceErrorCode.NETWORK


def test_session_runner_rejects_failed_subprocess() -> None:
    runner = AkshareSessionRunner(
        timeout_seconds=10.0,
        command=(sys.executable, "-c", "import sys; sys.exit(3)"),
    )
    request = DailyBarRequest("600000.SH", date(2026, 8, 1), date(2026, 8, 8))
    with pytest.raises(SourceAdapterError) as caught:
        runner.run(request, DataUpstream.TENCENT)
    assert caught.value.code is SourceErrorCode.INTERNAL_PROVIDER


def test_worker_mismatched_library_version_is_rejected_by_adapter() -> None:
    module = FakeModule(_rows(DataUpstream.SINA))
    adapter = AkshareDailyBarAdapter(
        DataUpstream.SINA,
        runner=FakeRunner(module, library_version="1.18.84"),
        module_loader=lambda: module,
        clock=_clock,
    )
    with pytest.raises(SourceAdapterError) as caught:
        adapter.fetch(DailyBarRequest("600000.SH", date(2026, 8, 1), date(2026, 8, 8)))
    assert caught.value.code is SourceErrorCode.INTERNAL_PROVIDER


def test_gateway_binds_request_window_and_unit_policy_in_provider_payload() -> None:
    gateway = AkshareMarketDataGateway(
        AkshareDailyBarAdapter(
            DataUpstream.TENCENT,
            runner=FakeRunner(FakeModule(_rows(DataUpstream.TENCENT))),
            module_loader=lambda: FakeModule(_rows(DataUpstream.TENCENT)),
            clock=_clock,
        )
    )
    chunk = FetchChunk(
        sequence=1,
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        scope=("600000.SH",),
        start=TradingDate(date(2026, 8, 1)),
        end=TradingDate(date(2026, 8, 8)),
        request_sha256="a" * 64,
    )
    response = gateway.fetch(chunk)
    provider_payload: Any = json.loads(response.raw_payload)["provider_payload"]
    assert provider_payload["unit_policy_version"] == AKSHARE_UNIT_POLICY_VERSION
    assert provider_payload["request"]["lookback_days"] == 15
    assert "resolution_policy_version" not in provider_payload


__all__ = ["FakeFrame", "FakeMarketDataGateway", "FakeModule", "FakeRunner", "_rows"]
