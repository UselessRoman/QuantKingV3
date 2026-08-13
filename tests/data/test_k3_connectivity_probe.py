from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from quantking.data.comparison.k3_connectivity import (
    K3_CONNECTIVITY_MAX_INVOCATIONS,
    K3_CONNECTIVITY_PROBE_MARKER,
    K3ConnectivityOutcome,
    fixed_k3_connectivity_plan,
    k3_connectivity_dry_run_payload,
    summarize_k3_connectivity,
)
from quantking.data.models import DataUpstream


def _success(upstream: DataUpstream) -> K3ConnectivityOutcome:
    return K3ConnectivityOutcome(
        upstream=upstream,
        succeeded=True,
        stable_code=None,
        message=None,
        complete=True,
        canonical_row_count=1,
        library_version="1.18.83",
        volume_unit="share",
        amount_unit="cny_yuan",
    )


def _failure(upstream: DataUpstream) -> K3ConnectivityOutcome:
    return K3ConnectivityOutcome(
        upstream=upstream,
        succeeded=False,
        stable_code="AKSHARE_NETWORK",
        message="AKShare upstream network request failed",
        complete=None,
        canonical_row_count=None,
        library_version=None,
        volume_unit=None,
        amount_unit=None,
    )


def test_connectivity_plan_freezes_three_minimal_invocations() -> None:
    payload = dict(k3_connectivity_dry_run_payload())
    assert payload["marker"] == K3_CONNECTIVITY_PROBE_MARKER
    assert payload["network_calls_made"] == 0
    assert payload["application_retries"] == 0
    assert payload["failure_policy"] == "RUN_ALL_BOUNDED_NO_RETRY"
    assert payload["writes_formal_data_root"] is False
    assert payload["target_date"] == "2026-06-11"
    assert payload["lookback_days"] == 15
    assert payload["market_data_endpoint_invocations"] == {
        "EASTMONEY": 1,
        "TENCENT": 1,
        "SINA": 1,
        "total": 3,
    }
    assert payload["akshare_locked_source_http_upper_bound"] == {
        "EASTMONEY": 1,
        "TENCENT": 3,
        "SINA": 2,
        "total": 6,
    }
    plan = fixed_k3_connectivity_plan()
    assert len(plan) == K3_CONNECTIVITY_MAX_INVOCATIONS == 3
    assert [item.upstream for item in plan] == [
        DataUpstream.EASTMONEY,
        DataUpstream.TENCENT,
        DataUpstream.SINA,
    ]
    assert {item.symbol for item in plan} == {"000001.SZ"}
    assert all((item.target_date - item.upstream_start).days == 15 for item in plan)


def test_connectivity_summary_records_all_three_outcomes_in_order() -> None:
    outcomes = (
        _failure(DataUpstream.EASTMONEY),
        _success(DataUpstream.TENCENT),
        _success(DataUpstream.SINA),
    )
    summary = dict(summarize_k3_connectivity(outcomes))
    assert summary["endpoint_invocations"] == 3
    assert summary["eastmoney_http_reachable"] is False
    assert summary["tencent_http_reachable"] is True
    assert summary["sina_http_reachable"] is True
    assert summary["outcomes"][0]["stable_code"] == "AKSHARE_NETWORK"
    assert summary["outcomes"][1]["canonical_row_count"] == 1


@pytest.mark.parametrize(
    "outcomes",
    [
        (_success(DataUpstream.EASTMONEY), _success(DataUpstream.TENCENT)),
        (
            _success(DataUpstream.TENCENT),
            _success(DataUpstream.EASTMONEY),
            _success(DataUpstream.SINA),
        ),
    ],
)
def test_connectivity_summary_rejects_wrong_count_or_order(
    outcomes: tuple[K3ConnectivityOutcome, ...],
) -> None:
    with pytest.raises(ValueError):
        summarize_k3_connectivity(outcomes)


def test_connectivity_outcome_rejects_mixed_success_facts() -> None:
    with pytest.raises(ValueError):
        K3ConnectivityOutcome(
            upstream=DataUpstream.SINA,
            succeeded=True,
            stable_code="AKSHARE_NETWORK",
            message="mixed",
            complete=True,
            canonical_row_count=1,
            library_version="1.18.83",
            volume_unit="share",
            amount_unit="cny_yuan",
        )
    with pytest.raises(ValueError):
        K3ConnectivityOutcome(
            upstream=DataUpstream.SINA,
            succeeded=False,
            stable_code="AKSHARE_NETWORK",
            message="failed",
            complete=True,
            canonical_row_count=None,
            library_version=None,
            volume_unit=None,
            amount_unit=None,
        )


def test_connectivity_dry_run_script_outputs_only_canonical_json_without_vendors() -> None:
    script = Path(__file__).parents[2] / "scripts" / "probe_s1_07k3_connectivity.py"
    bootstrap = (
        "import json, runpy, sys; "
        f"sys.argv = [{str(script)!r}, '--dry-run']; "
        f"module = runpy.run_path({str(script)!r}, run_name='k3_connectivity_dry_run'); "
        "module['main'](); "
        "vendors = sorted(name for name in sys.modules "
        "if name.partition('.')[0] in {'baostock', 'akshare'}); "
        "print(json.dumps({'vendors_in_modules': vendors}))"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", bootstrap],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    lines = completed.stdout.splitlines()
    assert len(lines) == 2
    payload = json.loads(lines[0])
    assert payload["marker"] == K3_CONNECTIVITY_PROBE_MARKER
    assert payload["network_calls_made"] == 0
    assert json.loads(lines[1]) == {"vendors_in_modules": []}
    assert completed.stderr == ""
