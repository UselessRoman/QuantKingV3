from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

import pytest

from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareEndpointResponse,
)
from quantking.data.adapters.sources.contracts import SourceAdapterError
from quantking.data.adapters.sources.parameters import DailyBarRequest
from quantking.data.adapters.sources.registry import AKSHARE_RUNTIME_UPSTREAMS
from quantking.data.models import DataUpstream

_PRICE_FIELDS = ("open", "high", "low", "close", "pre_close")
_SYMBOLS = ("600000.SH", "000001.SZ")


class _ProbeResult(Protocol):
    row_count: int
    records: tuple[Mapping[str, object], ...]
    complete: bool


@dataclass(frozen=True, slots=True)
class _FakeProbeResult:
    row_count: int
    records: tuple[Mapping[str, object], ...]
    complete: bool = True


def _probe_window(today: date) -> tuple[date, date]:
    """Return an inclusive window containing exactly 60 natural dates."""
    return today - timedelta(days=59), today


def _numeric_sample(result: AkshareEndpointResponse) -> dict[str, object]:
    if not result.records:
        return {}
    row = result.records[0]
    return {
        field: row[field] for field in ("stock_code", "date", *_PRICE_FIELDS, "volume", "amount")
    }


def _probe_failures(
    results: Mapping[tuple[DataUpstream, str], _ProbeResult],
    errors: Mapping[tuple[DataUpstream, str], str],
) -> tuple[str, ...]:
    failures: list[str] = []
    for upstream in AKSHARE_RUNTIME_UPSTREAMS:
        for symbol in _SYMBOLS:
            key = (upstream, symbol)
            if key in errors:
                failures.append(f"CALL_FAILED:{upstream.value}:{symbol}:{errors[key]}")
                continue
            result = results.get(key)
            if result is None:
                failures.append(f"MISSING_RESULT:{upstream.value}:{symbol}")
                continue
            if result.row_count <= 0:
                failures.append(f"ZERO_UPSTREAM_ROWS:{upstream.value}:{symbol}")
            if result.complete is not True:
                failures.append(f"PARTIAL_RESULT:{upstream.value}:{symbol}")
            if not result.records:
                failures.append(f"NO_CANONICAL_ROWS:{upstream.value}:{symbol}")
    return tuple(failures)


def test_probe_failure_aggregation_is_stable_and_does_not_hide_partial_failures() -> None:
    one_row = _FakeProbeResult(
        row_count=1,
        records=(
            {
                "date": "2026-08-07",
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "close": 10.2,
                "pre_close": 10.0,
                "volume": 100,
                "amount": 1_000.0,
            },
        ),
    )
    zero_rows = _FakeProbeResult(row_count=0, records=())
    partial = _FakeProbeResult(row_count=1, records=one_row.records, complete=False)
    results: dict[tuple[DataUpstream, str], _ProbeResult] = {
        (DataUpstream.TENCENT, "600000.SH"): one_row,
        (DataUpstream.SINA, "600000.SH"): zero_rows,
        (DataUpstream.TENCENT, "000001.SZ"): one_row,
        (DataUpstream.SINA, "000001.SZ"): partial,
    }
    errors = {(DataUpstream.TENCENT, "000001.SZ"): "AKSHARE_NETWORK"}

    assert _probe_failures(results, errors) == (
        "CALL_FAILED:TENCENT:000001.SZ:AKSHARE_NETWORK",
        "ZERO_UPSTREAM_ROWS:SINA:600000.SH",
        "NO_CANONICAL_ROWS:SINA:600000.SH",
        "PARTIAL_RESULT:SINA:000001.SZ",
    )


def test_probe_window_contains_exactly_60_natural_dates() -> None:
    start, end = _probe_window(date(2026, 8, 9))

    assert start == date(2026, 6, 11)
    assert end == date(2026, 8, 9)
    assert (end - start).days + 1 == 60


@pytest.mark.real_akshare
def test_fixed_akshare_daily_bar_probe() -> None:
    """Four calls maximum: two fixed symbols times two upstreams, with no retry."""
    if os.environ.get("QUANTKING_RUN_REAL_AKSHARE") != "1":
        pytest.skip("fixed real AKShare probe requires explicit authorization")
    start, end = _probe_window(date.today())
    results: dict[tuple[DataUpstream, str], AkshareEndpointResponse] = {}
    errors: dict[tuple[DataUpstream, str], str] = {}
    for upstream in AKSHARE_RUNTIME_UPSTREAMS:
        adapter = AkshareDailyBarAdapter(upstream)
        for symbol in _SYMBOLS:
            try:
                result = adapter.fetch(DailyBarRequest(symbol, start, end))
            except SourceAdapterError as error:
                errors[(upstream, symbol)] = error.code.value
                print(f"{upstream.value} {symbol} error={error.code.value}")
                continue
            except Exception:
                errors[(upstream, symbol)] = "AKSHARE_UNEXPECTED_INTERNAL"
                print(f"{upstream.value} {symbol} error=AKSHARE_UNEXPECTED_INTERNAL")
                continue
            results[(upstream, symbol)] = result
            dates = tuple(str(record["date"]) for record in result.records)
            field_types = (
                {key: type(value).__name__ for key, value in result.records[0].items()}
                if result.records
                else {}
            )
            date_range = f"{min(dates)}..{max(dates)}" if dates else "NONE"
            print(
                f"{upstream.value} {symbol} endpoint={result.lineage.endpoint} "
                f"version={result.lineage.library_version} upstream_rows={result.row_count} "
                f"canonical_rows={len(result.records)} complete={result.complete} "
                f"target_range={result.request_window.target_start}.."
                f"{result.request_window.target_end} "
                f"upstream_range={result.request_window.upstream_start}.."
                f"{result.request_window.upstream_end} "
                f"lookback_days={result.request_window.lookback_days} "
                f"range={date_range} fields={sorted(field_types.items())} "
                f"volume_unit={result.volume_unit.value} amount_unit={result.amount_unit.value} "
                f"unit_policy_version={result.unit_policy_version} "
                f"numeric_sample={json.dumps(_numeric_sample(result), sort_keys=True)}"
            )
    failures = _probe_failures(results, errors)
    print(
        "PROBE_FAILURES "
        f"count={len(failures)} details={json.dumps(failures, separators=(',', ':'))}"
    )
    if failures:
        pytest.fail(
            f"fixed AKShare probe failed with {len(failures)} blocking issue(s)", pytrace=False
        )
