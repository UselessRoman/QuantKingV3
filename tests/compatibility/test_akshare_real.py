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


def _ratio_summary(values: list[float]) -> dict[str, object]:
    if not values:
        return {"count": 0, "min": None, "max": None, "conclusion": "NO_COMMON_NONZERO"}
    minimum = min(values)
    maximum = max(values)
    if 0.99 <= minimum <= maximum <= 1.01:
        conclusion = "CONSISTENT_NUMERIC_SCALE"
    elif (99.0 <= minimum <= maximum <= 101.0) or (0.0099 <= minimum <= maximum <= 0.0101):
        conclusion = "POSSIBLE_100X_SCALE_DIFFERENCE"
    else:
        conclusion = "UNRESOLVED_SCALE_DIFFERENCE"
    return {
        "count": len(values),
        "min": round(minimum, 8),
        "max": round(maximum, 8),
        "conclusion": conclusion,
    }


def _same_sample_summary(
    primary: tuple[Mapping[str, object], ...],
    secondary: tuple[Mapping[str, object], ...],
) -> dict[str, object]:
    by_primary = {str(row["date"]): row for row in primary}
    by_secondary = {str(row["date"]): row for row in secondary}
    common_dates = sorted(set(by_primary) & set(by_secondary))
    price_differences = [
        abs(float(by_primary[day][field]) - float(by_secondary[day][field]))
        for day in common_dates
        for field in _PRICE_FIELDS
    ]
    volume_ratios = [
        float(by_primary[day]["volume"]) / float(by_secondary[day]["volume"])
        for day in common_dates
        if float(by_secondary[day]["volume"]) != 0.0
    ]
    amount_ratios = [
        float(by_primary[day]["amount"]) / float(by_secondary[day]["amount"])
        for day in common_dates
        if float(by_secondary[day]["amount"]) != 0.0
    ]
    volume_differences = [
        abs(float(by_primary[day]["volume"]) - float(by_secondary[day]["volume"]))
        for day in common_dates
    ]
    amount_differences = [
        abs(float(by_primary[day]["amount"]) - float(by_secondary[day]["amount"]))
        for day in common_dates
    ]
    return {
        "common_date_count": len(common_dates),
        "date_range": f"{common_dates[0]}..{common_dates[-1]}" if common_dates else "NONE",
        "price_max_abs_difference": round(max(price_differences), 8) if price_differences else None,
        "volume_eastmoney_to_tencent": _ratio_summary(volume_ratios),
        "amount_eastmoney_to_tencent": _ratio_summary(amount_ratios),
        "volume_max_abs_difference": round(max(volume_differences), 8)
        if volume_differences
        else None,
        "amount_max_abs_difference": round(max(amount_differences), 8)
        if amount_differences
        else None,
    }


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
    for upstream in DataUpstream:
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
    for symbol in _SYMBOLS:
        eastmoney = results.get((DataUpstream.EASTMONEY, symbol))
        tencent = results.get((DataUpstream.TENCENT, symbol))
        if (
            eastmoney is None
            or tencent is None
            or eastmoney.row_count <= 0
            or tencent.row_count <= 0
            or eastmoney.complete is not True
            or tencent.complete is not True
            or not eastmoney.records
            or not tencent.records
        ):
            failures.append(f"AB_SUMMARY_UNAVAILABLE:{symbol}")
            continue
        if _same_sample_summary(eastmoney.records, tencent.records)["common_date_count"] == 0:
            failures.append(f"AB_NO_COMMON_DATES:{symbol}")
    return tuple(failures)


def test_same_sample_summary_exposes_scale_evidence_without_guessing_units() -> None:
    primary = (
        {
            "date": "2026-08-07",
            "open": 10.0,
            "high": 10.5,
            "low": 9.9,
            "close": 10.2,
            "pre_close": 10.0,
            "volume": 10_000,
            "amount": 20_000.0,
        },
    )
    secondary = (
        {
            "date": "2026-08-07",
            "open": 10.0,
            "high": 10.5,
            "low": 9.9,
            "close": 10.1,
            "pre_close": 10.0,
            "volume": 100,
            "amount": 20_000.0,
        },
    )

    summary = _same_sample_summary(primary, secondary)

    assert summary["common_date_count"] == 1
    assert summary["price_max_abs_difference"] == 0.1
    assert summary["volume_eastmoney_to_tencent"] == {
        "count": 1,
        "min": 100.0,
        "max": 100.0,
        "conclusion": "POSSIBLE_100X_SCALE_DIFFERENCE",
    }
    assert summary["amount_eastmoney_to_tencent"] == {
        "count": 1,
        "min": 1.0,
        "max": 1.0,
        "conclusion": "CONSISTENT_NUMERIC_SCALE",
    }
    assert summary["volume_max_abs_difference"] == 9_900.0
    assert summary["amount_max_abs_difference"] == 0.0


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
        (DataUpstream.EASTMONEY, "600000.SH"): one_row,
        (DataUpstream.TENCENT, "600000.SH"): one_row,
        (DataUpstream.SINA, "600000.SH"): zero_rows,
        (DataUpstream.EASTMONEY, "000001.SZ"): one_row,
        (DataUpstream.SINA, "000001.SZ"): partial,
    }
    errors = {(DataUpstream.TENCENT, "000001.SZ"): "AKSHARE_NETWORK"}

    assert _probe_failures(results, errors) == (
        "CALL_FAILED:TENCENT:000001.SZ:AKSHARE_NETWORK",
        "ZERO_UPSTREAM_ROWS:SINA:600000.SH",
        "NO_CANONICAL_ROWS:SINA:600000.SH",
        "PARTIAL_RESULT:SINA:000001.SZ",
        "AB_SUMMARY_UNAVAILABLE:000001.SZ",
    )

    different_date = _FakeProbeResult(
        row_count=1,
        records=({**dict(one_row.records[0]), "date": "2026-08-06"},),
    )
    complete_results: dict[tuple[DataUpstream, str], _ProbeResult] = {
        (upstream, symbol): one_row for upstream in DataUpstream for symbol in _SYMBOLS
    }
    complete_results[(DataUpstream.TENCENT, "000001.SZ")] = different_date
    assert _probe_failures(complete_results, {}) == ("AB_NO_COMMON_DATES:000001.SZ",)


def test_probe_window_contains_exactly_60_natural_dates() -> None:
    start, end = _probe_window(date(2026, 8, 9))

    assert start == date(2026, 6, 11)
    assert end == date(2026, 8, 9)
    assert (end - start).days + 1 == 60


@pytest.mark.real_akshare
def test_fixed_akshare_daily_bar_probe() -> None:
    """Six calls maximum: two fixed symbols times three upstreams, with no retry."""
    if os.environ.get("QUANTKING_RUN_REAL_AKSHARE") != "1":
        pytest.skip("fixed real AKShare probe requires explicit authorization")
    start, end = _probe_window(date.today())
    results: dict[tuple[DataUpstream, str], AkshareEndpointResponse] = {}
    errors: dict[tuple[DataUpstream, str], str] = {}
    for upstream in DataUpstream:
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
    for symbol in _SYMBOLS:
        eastmoney = results.get((DataUpstream.EASTMONEY, symbol))
        tencent = results.get((DataUpstream.TENCENT, symbol))
        if eastmoney is None or tencent is None:
            print(f"AB_SUMMARY {symbol} status=UNAVAILABLE")
            continue
        summary = _same_sample_summary(eastmoney.records, tencent.records)
        print(
            f"AB_SUMMARY {symbol} status=OBSERVED_NOT_FROZEN "
            f"summary={json.dumps(summary, sort_keys=True, separators=(',', ':'))}"
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
