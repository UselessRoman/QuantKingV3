from __future__ import annotations

from decimal import Decimal

import pytest

from quantking.data.adapters.sources.akshare import AKSHARE_SESSION_TIMEOUT_SECONDS
from quantking.data.adapters.sources.baostock import BAOSTOCK_SESSION_TIMEOUT_SECONDS
from quantking.data.adapters.sources.units import (
    AKSHARE_UNIT_POLICY_VERSION,
    BAOSTOCK_UNIT_POLICY_VERSION,
)
from quantking.data.eod.release import K4C_REPRESENTATIVE_SYMBOLS
from quantking.data.models import DataProvider, DataUpstream
from quantking.history.stage1.k4c_probe import (
    K4C_MAX_REPORTED_DIVERGENCES,
    K4C_PROBE_MARKER,
    K4CProbeInvocation,
    fixed_k4c_probe_plan,
    k4c_cross_source_measurement,
    k4c_probe_dry_run_payload,
    summarize_k4c_probe,
)

_ROLE_BY_UPSTREAM = {
    DataUpstream.BAOSTOCK: "A",
    DataUpstream.TENCENT: "B",
    DataUpstream.SINA: "C",
}
_ENDPOINT_BY_UPSTREAM = {
    DataUpstream.BAOSTOCK: "query_history_k_data_plus",
    DataUpstream.TENCENT: "stock_zh_a_hist_tx",
    DataUpstream.SINA: "stock_zh_a_daily",
}


def test_dry_run_payload_freezes_k4c_three_source_contract() -> None:
    payload = dict(k4c_probe_dry_run_payload())
    assert payload["schema_version"] == "s1-07k4c-probe-dry-run.v1"
    assert payload["authorization_marker"] == K4C_PROBE_MARKER
    assert payload["dataset"] == "daily_bar_unadjusted"
    assert payload["schema"] == "daily_bar_unadjusted.v1"
    assert payload["sample"]["symbols"] == list(K4C_REPRESENTATIVE_SYMBOLS)
    assert payload["target_window"] == {"start": "2026-06-11", "end": "2026-08-09"}
    assert payload["lookback_days"] == 15
    assert payload["chunk_policy"] == {
        "max_days_per_chunk": 31,
        "max_symbols_per_chunk": 1,
    }
    budget = payload["network_budget"]
    assert budget["a_baostock_endpoint_invocations"] == 12
    assert budget["b_tencent_endpoint_invocations"] == 12
    assert budget["max_c_sina_endpoint_invocations"] == 12
    assert budget["max_endpoint_invocations"] == 36
    assert budget["estimated_max_baostock_protocol_messages"] == 36
    assert budget["estimated_max_akshare_http_requests"] == 60
    assert budget["application_retries"] == 0
    assert budget["fail_fast"] is True
    assert payload["hard_timeouts_seconds"] == {
        "baostock_session": BAOSTOCK_SESSION_TIMEOUT_SECONDS,
        "akshare_session": AKSHARE_SESSION_TIMEOUT_SECONDS,
    }
    assert payload["hard_timeouts_seconds"] == {
        "baostock_session": 20.0,
        "akshare_session": 60.0,
    }
    assert payload["unit_policies"] == {
        "baostock": BAOSTOCK_UNIT_POLICY_VERSION,
        "akshare": AKSHARE_UNIT_POLICY_VERSION,
    }
    assert payload["requires_real_network_authorization"] is True
    assert len(payload["invocations"]) == 36


def test_fixed_plan_maps_three_roles_and_frozen_endpoints() -> None:
    plan = fixed_k4c_probe_plan()
    assert len(plan) == 36
    assert {item.symbol for item in plan} == set(K4C_REPRESENTATIVE_SYMBOLS)
    assert {item.role for item in plan} == {"A", "B", "C"}
    for invocation in plan:
        assert isinstance(invocation, K4CProbeInvocation)
        assert invocation.role == _ROLE_BY_UPSTREAM[invocation.upstream]
        assert invocation.endpoint == _ENDPOINT_BY_UPSTREAM[invocation.upstream]
        if invocation.upstream is DataUpstream.BAOSTOCK:
            assert invocation.provider is DataProvider.BAOSTOCK
        else:
            assert invocation.provider is DataProvider.AKSHARE
        assert (invocation.target_start - invocation.upstream_start).days == 15
        assert invocation.upstream_end == invocation.target_end
        assert invocation.target_start <= invocation.target_end


def test_summarize_k4c_probe_counts_ok_failures_and_missing() -> None:
    plan = fixed_k4c_probe_plan()
    results = {}
    for invocation in plan:
        if invocation.role == "A":
            results[invocation] = {"status": "OK", "row_count": 3}
        elif invocation.role == "B":
            results[invocation] = {"status": "AKSHARE_RATE_LIMITED"}
        # role C is deliberately missing

    summary = dict(summarize_k4c_probe(results))

    assert summary["schema_version"] == "s1-07k4c-probe-summary.v1"
    assert summary["marker"] == K4C_PROBE_MARKER
    assert summary["symbols"] == 6
    assert summary["any_failure_means_stop"] is True
    assert summary["per_upstream"]["BAOSTOCK"] == {
        "ok": 12,
        "failed": 0,
        "rows": 36,
        "invocations": 12,
    }
    assert summary["per_upstream"]["TENCENT"] == {
        "ok": 0,
        "failed": 12,
        "rows": 0,
        "invocations": 12,
    }
    assert "SINA" not in summary["per_upstream"]
    assert summary["missing_invocations"] == sorted(K4C_REPRESENTATIVE_SYMBOLS)


def _row(*, close: float = 10.5, volume: int = 1000, amount: float = 10500.0) -> dict[str, object]:
    return {
        "code": "000001.SZ",
        "trade_date": "2026-06-11",
        "open": 10.0,
        "high": 10.8,
        "low": 9.9,
        "close": close,
        "pre_close": 10.1,
        "volume": volume,
        "amount": amount,
    }


def test_cross_source_measurement_counts_exact_three_role_agreement() -> None:
    measurement = dict(
        k4c_cross_source_measurement(
            {
                "A": {("000001.SZ", "2026-06-11"): _row()},
                "B": {("000001.SZ", "2026-06-11"): _row()},
                "C": {("000001.SZ", "2026-06-11"): _row()},
            }
        )
    )

    assert measurement["schema_version"] == "s1-07k4c-measurement.v1"
    assert measurement["held_rows_total"] == 1
    assert measurement["rows_held_by_role_count"] == {"3": 1}
    for field in ("open", "high", "low", "close", "pre_close", "volume", "amount"):
        assert measurement["fields"][field]["compared_rows"] == 1
        assert measurement["fields"][field]["exact_rows"] == 1
        assert measurement["fields"][field]["divergent_rows"] == 0
        assert measurement["fields"][field]["divergences"] == []
    assert measurement["fields"]["volume"]["max_absolute_difference"] is None
    assert measurement["fields"]["amount"]["max_relative_difference"] is None


def test_cross_source_measurement_measures_volume_amount_differences() -> None:
    measurement = dict(
        k4c_cross_source_measurement(
            {
                "A": {("000001.SZ", "2026-06-11"): _row(volume=1234567, amount=13000000.0)},
                "B": {("000001.SZ", "2026-06-11"): _row(volume=1234500, amount=12999950.0)},
                "C": {("000001.SZ", "2026-06-11"): _row(volume=1234567, amount=13000000.0)},
            }
        )
    )

    assert measurement["fields"]["close"]["divergent_rows"] == 0
    volume = measurement["fields"]["volume"]
    assert volume["divergent_rows"] == 1
    assert volume["max_absolute_difference"] == "67"
    assert volume["max_relative_difference"] is not None
    assert Decimal("0") < Decimal(volume["max_relative_difference"]) < Decimal("1")
    amount = measurement["fields"]["amount"]
    assert amount["divergent_rows"] == 1
    assert amount["max_absolute_difference"] == "50"
    assert amount["divergences"][0]["values"] == {
        "A": "13000000",
        "B": "12999950",
        "C": "13000000",
    }


def test_cross_source_measurement_never_compares_single_held_rows() -> None:
    measurement = dict(
        k4c_cross_source_measurement(
            {
                "A": {
                    ("000001.SZ", "2026-06-11"): _row(),
                    ("000001.SZ", "2026-06-12"): _row(),
                },
                "B": {("000001.SZ", "2026-06-11"): _row()},
                "C": {},
            }
        )
    )

    assert measurement["held_rows_total"] == 2
    assert measurement["rows_held_by_role_count"] == {"2": 1, "1": 1}
    for field in ("open", "high", "low", "close", "pre_close", "volume", "amount"):
        assert measurement["fields"][field]["compared_rows"] == 1
        assert measurement["fields"][field]["exact_rows"] == 1


def test_cross_source_measurement_float_noise_is_divergent_under_zero_tolerance() -> None:
    measurement = dict(
        k4c_cross_source_measurement(
            {
                "A": {("000001.SZ", "2026-06-11"): _row(close=12.34)},
                "B": {("000001.SZ", "2026-06-11"): _row(close=12.3400000001)},
            }
        )
    )

    close = measurement["fields"]["close"]
    assert close["divergent_rows"] == 1
    assert close["divergences"][0]["values"] == {"A": "12.34", "B": "12.3400000001"}


def test_cross_source_measurement_bounds_reported_divergences() -> None:
    rows_a = {(f"{index:06d}.SZ", "2026-06-11"): _row(volume=2000 + index) for index in range(30)}
    rows_b = {(f"{index:06d}.SZ", "2026-06-11"): _row(volume=1000) for index in range(30)}
    measurement = dict(k4c_cross_source_measurement({"A": rows_a, "B": rows_b}))

    volume = measurement["fields"]["volume"]
    assert volume["divergent_rows"] == 30
    assert len(volume["divergences"]) == K4C_MAX_REPORTED_DIVERGENCES


def test_cross_source_measurement_rejects_invalid_roles_and_rows() -> None:
    with pytest.raises(ValueError, match="single-letter"):
        k4c_cross_source_measurement({"AA": {}})
    with pytest.raises(TypeError, match="row mappings"):
        k4c_cross_source_measurement({"A": [1, 2]})
