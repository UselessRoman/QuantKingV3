from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

import pytest

from quantking.data.adapters.sources.akshare import AkshareDailyBarAdapter
from quantking.data.adapters.sources.contracts import (
    AmountUnit,
    SourceAdapterError,
    SourceErrorCode,
    VolumeUnit,
)
from quantking.data.adapters.sources.parameters import DailyBarRequest
from quantking.data.adapters.sources.request_window import AkshareRequestWindow
from quantking.data.models import DataUpstream

_ENV_GUARD = "QUANTKING_RUN_REAL_AKSHARE_EASTMONEY_RECHECK"
_SYMBOLS = ("600000.SH", "000001.SZ")
_TARGET_START = date(2026, 6, 11)
_TARGET_END = date(2026, 8, 9)
_UPSTREAM_START = date(2026, 5, 27)
_UPSTREAM_END = date(2026, 8, 9)
_NUMERIC_FIELDS = (
    "stock_code",
    "date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
)


class _ProbeLineage(Protocol):
    endpoint: str
    library_version: str


class _ProbeResponse(Protocol):
    request: DailyBarRequest
    request_window: AkshareRequestWindow
    lineage: _ProbeLineage
    records: tuple[Mapping[str, object], ...]
    row_count: int
    complete: bool
    volume_unit: VolumeUnit
    amount_unit: AmountUnit
    unit_policy_version: str


class _ProbeAdapter(Protocol):
    def fetch(self, request: DailyBarRequest) -> _ProbeResponse: ...


@dataclass(frozen=True, slots=True)
class _FakeLineage:
    endpoint: str = "stock_zh_a_hist"
    library_version: str = "1.18.83"


@dataclass(frozen=True, slots=True)
class _FakeResponse:
    request: DailyBarRequest
    records: tuple[Mapping[str, object], ...]
    row_count: int = 52
    complete: bool = True
    volume_unit: VolumeUnit = VolumeUnit.LOT_100_SHARES
    amount_unit: AmountUnit = AmountUnit.CNY_YUAN
    unit_policy_version: str = "akshare-1.18.83-returned-units.v1"
    lineage: _FakeLineage = _FakeLineage()

    @property
    def request_window(self) -> AkshareRequestWindow:
        return self.request.request_window


class _FakeAdapter:
    def __init__(
        self,
        responses: Mapping[str, _ProbeResponse | SourceAdapterError],
    ) -> None:
        self._responses = responses
        self.requests: list[DailyBarRequest] = []

    def fetch(self, request: DailyBarRequest) -> _ProbeResponse:
        self.requests.append(request)
        result = self._responses[request.symbol]
        if isinstance(result, SourceAdapterError):
            raise result
        return result


def _request(symbol: str) -> DailyBarRequest:
    return DailyBarRequest(symbol, _TARGET_START, _TARGET_END)


def _target_record(symbol: str) -> Mapping[str, object]:
    return {
        "stock_code": symbol,
        "date": _TARGET_START.isoformat(),
        "open": 10.0,
        "high": 10.5,
        "low": 9.9,
        "close": 10.2,
        "pre_close": 10.0,
        "volume": 123,
        "amount": 125_000.0,
    }


def _collect(
    adapter: _ProbeAdapter,
) -> tuple[dict[str, _ProbeResponse], dict[str, str]]:
    results: dict[str, _ProbeResponse] = {}
    errors: dict[str, str] = {}
    for symbol in _SYMBOLS:
        try:
            results[symbol] = adapter.fetch(_request(symbol))
        except SourceAdapterError as error:
            errors[symbol] = error.code.value
        except Exception:
            errors[symbol] = "AKSHARE_UNEXPECTED_INTERNAL"
    return results, errors


def _failures(
    results: Mapping[str, _ProbeResponse],
    errors: Mapping[str, str],
) -> tuple[str, ...]:
    failures: list[str] = []
    for symbol in _SYMBOLS:
        if symbol in errors:
            failures.append(f"CALL_FAILED:{symbol}:{errors[symbol]}")
            continue
        result = results.get(symbol)
        if result is None:
            failures.append(f"MISSING_RESULT:{symbol}")
            continue
        window = result.request_window
        if (
            result.request.symbol != symbol
            or result.request.start_date != _TARGET_START
            or result.request.end_date != _TARGET_END
            or window.target_start != _TARGET_START
            or window.target_end != _TARGET_END
            or window.upstream_start != _UPSTREAM_START
            or window.upstream_end != _UPSTREAM_END
            or window.lookback_days != 15
        ):
            failures.append(f"REQUEST_WINDOW_MISMATCH:{symbol}")
        if result.lineage.endpoint != "stock_zh_a_hist":
            failures.append(f"ENDPOINT_MISMATCH:{symbol}")
        if result.lineage.library_version != "1.18.83":
            failures.append(f"LIBRARY_VERSION_MISMATCH:{symbol}")
        if result.unit_policy_version != "akshare-1.18.83-returned-units.v1":
            failures.append(f"UNIT_POLICY_VERSION_MISMATCH:{symbol}")
        if result.row_count <= 0:
            failures.append(f"ZERO_UPSTREAM_ROWS:{symbol}")
        if not result.records:
            failures.append(f"NO_CANONICAL_ROWS:{symbol}")
        if result.complete is not True:
            failures.append(f"PARTIAL_RESULT:{symbol}")
        target = next(
            (
                record
                for record in result.records
                if record.get("date") == _TARGET_START.isoformat()
            ),
            None,
        )
        pre_close = None if target is None else target.get("pre_close")
        if target is None or isinstance(pre_close, bool) or not isinstance(pre_close, (int, float)):
            failures.append(f"TARGET_START_PRE_CLOSE_MISSING:{symbol}")
        if any(
            not (
                _TARGET_START.isoformat() <= str(record.get("date", "")) <= _TARGET_END.isoformat()
            )
            for record in result.records
        ):
            failures.append(f"CANONICAL_CONTEXT_LEAK:{symbol}")
        if result.volume_unit is not VolumeUnit.LOT_100_SHARES:
            failures.append(f"VOLUME_UNIT_MISMATCH:{symbol}")
        if result.amount_unit is not AmountUnit.CNY_YUAN:
            failures.append(f"AMOUNT_UNIT_MISMATCH:{symbol}")
    return tuple(failures)


def _numeric_sample(result: _ProbeResponse) -> dict[str, object]:
    if not result.records:
        return {}
    record = result.records[0]
    return {field: record[field] for field in _NUMERIC_FIELDS}


def _print_evidence(
    results: Mapping[str, _ProbeResponse],
    errors: Mapping[str, str],
) -> None:
    for symbol in _SYMBOLS:
        if symbol in errors:
            print(f"EASTMONEY {symbol} error={errors[symbol]}")
            continue
        result = results.get(symbol)
        if result is None:
            print(f"EASTMONEY {symbol} error=MISSING_RESULT")
            continue
        dates = tuple(str(record["date"]) for record in result.records)
        date_range = f"{min(dates)}..{max(dates)}" if dates else "NONE"
        field_types = (
            {key: type(value).__name__ for key, value in result.records[0].items()}
            if result.records
            else {}
        )
        window = result.request_window
        print(
            f"EASTMONEY {symbol} endpoint={result.lineage.endpoint} "
            f"version={result.lineage.library_version} "
            f"target_range={window.target_start}..{window.target_end} "
            f"upstream_range={window.upstream_start}..{window.upstream_end} "
            f"lookback_days={window.lookback_days} upstream_rows={result.row_count} "
            f"canonical_rows={len(result.records)} complete={result.complete} "
            f"canonical_range={date_range} fields={sorted(field_types.items())} "
            f"volume_unit={result.volume_unit.value} amount_unit={result.amount_unit.value} "
            f"unit_policy_version={result.unit_policy_version} "
            f"numeric_sample={json.dumps(_numeric_sample(result), sort_keys=True)}"
        )


def test_offline_collection_attempts_exact_fixed_set_and_does_not_hide_errors() -> None:
    first = _request("600000.SH")
    second = _request("000001.SZ")
    adapter = _FakeAdapter(
        {
            "600000.SH": SourceAdapterError(SourceErrorCode.NETWORK),
            "000001.SZ": _FakeResponse(second, (_target_record("000001.SZ"),)),
        }
    )

    results, errors = _collect(adapter)

    assert tuple(request.symbol for request in adapter.requests) == _SYMBOLS
    assert len(adapter.requests) == 2
    assert all(
        request.request_window.upstream_start == _UPSTREAM_START for request in adapter.requests
    )
    assert first.request_window.upstream_end == _UPSTREAM_END
    assert errors == {"600000.SH": "AKSHARE_NETWORK"}
    assert "000001.SZ" in results
    assert _failures(results, errors) == ("CALL_FAILED:600000.SH:AKSHARE_NETWORK",)


def test_offline_success_contract_requires_complete_target_only_canonical() -> None:
    responses = {
        symbol: _FakeResponse(_request(symbol), (_target_record(symbol),)) for symbol in _SYMBOLS
    }
    results, errors = _collect(_FakeAdapter(responses))

    assert errors == {}
    assert _failures(results, errors) == ()


def test_recheck_env_guard_skips_before_adapter_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_ENV_GUARD, raising=False)

    with pytest.raises(pytest.skip.Exception):
        test_fixed_eastmoney_recheck()


def test_default_quality_gate_excludes_real_akshare_marker() -> None:
    quality = (Path(__file__).parents[2] / "scripts" / "quality.ps1").read_text(encoding="utf-8")

    assert "not real_akshare" in quality


@pytest.mark.real_akshare
def test_fixed_eastmoney_recheck() -> None:
    """Exactly two EastMoney endpoint invocations; no application retry or fallback."""
    if os.environ.get(_ENV_GUARD) != "1":
        pytest.skip("fixed EastMoney recheck requires explicit authorization")
    adapter = AkshareDailyBarAdapter(DataUpstream.EASTMONEY)

    results, errors = _collect(adapter)
    _print_evidence(results, errors)
    failures = _failures(results, errors)
    print(
        "EASTMONEY_RECHECK_FAILURES "
        f"count={len(failures)} details={json.dumps(failures, separators=(',', ':'))}"
    )
    if failures:
        pytest.fail(
            f"fixed EastMoney recheck failed with {len(failures)} blocking issue(s)",
            pytrace=False,
        )
