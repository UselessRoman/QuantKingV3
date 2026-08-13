from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from quantking.data.comparison.k3_probe import (
    K3_REPRESENTATIVE_MAX_INVOCATIONS,
    K3_REPRESENTATIVE_PROBE_MARKER,
    K3_REPRESENTATIVE_SYMBOLS,
    K3ProbeSourceResult,
    code_segment,
    fixed_k3_representative_probe_plan,
    k3_representative_dry_run_payload,
    summarize_k3_probe,
)
from quantking.data.models import DataProvider, DataUpstream


def _record(
    *,
    code: str = "600000.SH",
    day: date = date(2026, 8, 7),
    close: float = 10.0,
    volume: int = 100,
    amount: float = 1000.0,
) -> dict[str, object]:
    return {
        "code": code,
        "trade_date": day,
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": close,
        "pre_close": 10.0,
        "volume": volume,
        "amount": amount,
    }


def _source(role: str, records: tuple[dict[str, object], ...]) -> K3ProbeSourceResult:
    return K3ProbeSourceResult(
        role=role,
        provider=(DataProvider.BAOSTOCK if role == "A" else DataProvider.AKSHARE),
        upstream=(
            DataUpstream.BAOSTOCK
            if role == "A"
            else {
                "B": DataUpstream.EASTMONEY,
                "C": DataUpstream.TENCENT,
                "D": DataUpstream.SINA,
            }[role]
        ),
        library_versions=("0.9.3",) if role == "A" else ("1.18.83",),
        contract_versions=(
            ("baostock-daily-bar.v1",) if role == "A" else ("akshare-daily-bar.v1",)
        ),
        volume_units=("share",),
        amount_units=("cny_yuan",),
        unadjusted=True,
        complete=True,
        upstream_row_count=len(records),
        records=records,
        observed_isST=("0", "1") if role == "A" else None,
    )


def test_representative_plan_freezes_six_symbols_twenty_four_invocations() -> None:
    payload = dict(k3_representative_dry_run_payload())
    assert payload["marker"] == K3_REPRESENTATIVE_PROBE_MARKER
    assert payload["mode"] == "dry-run"
    assert payload["network_calls_made"] == 0
    assert payload["application_retries"] == 0
    assert payload["failure_policy"] == "STOP_ON_FIRST_FAILURE"
    assert payload["writes_formal_data_root"] is False
    assert payload["target_start"] == "2026-05-11"
    assert payload["target_end"] == "2026-08-09"
    assert payload["market_data_endpoint_invocations"] == {
        "BAOSTOCK": 6,
        "EASTMONEY": 6,
        "TENCENT": 6,
        "SINA": 6,
        "total": 24,
    }
    assert payload["baostock_protocol_message_upper_bound"] == {
        "login": 6,
        "query": 6,
        "logout_attempt": 6,
        "total": 18,
    }
    assert payload["akshare_locked_source_http_upper_bound"] == {
        "EASTMONEY": 6,
        "TENCENT": 18,
        "SINA": 12,
        "total": 36,
    }
    assert payload["excluded_scope"] == ["beijing_stock_exchange", "delisted_securities"]
    assert payload["threshold_candidate_active"] is False
    assert payload["representative_evidence_sufficient"] is False
    plan = fixed_k3_representative_probe_plan()
    assert len(plan) == K3_REPRESENTATIVE_MAX_INVOCATIONS == 24
    assert {item.symbol for item in plan} == set(K3_REPRESENTATIVE_SYMBOLS)
    assert {item.role for item in plan} == {"A", "B", "C", "D"}
    assert all((item.target_start - item.upstream_start).days == 15 for item in plan)
    assert tuple(item.sequence for item in plan) == tuple(range(1, 25))


def test_representative_symbols_cover_four_code_segments() -> None:
    segments = {code_segment(symbol) for symbol in K3_REPRESENTATIVE_SYMBOLS}
    assert segments == {"sh_main", "sh_star", "sz_main", "sz_chinext"}
    assert code_segment("430047.BJ") == "other"


def test_representative_summary_reports_bounded_dimension_evidence() -> None:
    records = (
        _record(code="000001.SZ", volume=100, amount=1000.0),
        _record(code="600000.SH", volume=0, amount=0.0, day=date(2026, 8, 8)),
        _record(code="688981.SH", volume=1, amount=8.1),
        _record(code="300750.SZ", volume=5000, amount=100_000.0),
    )
    results = tuple(_source(role, records) for role in ("A", "B", "C", "D"))
    summary = dict(
        summarize_k3_probe(
            results,
            marker=K3_REPRESENTATIVE_PROBE_MARKER,
            schema_version="s1-07k3b-representative-evidence.v1",
        )
    )
    assert summary["marker"] == K3_REPRESENTATIVE_PROBE_MARKER
    assert summary["endpoint_invocations"] == 16
    dimension = summary["dimension_evidence"]
    assert dimension["code_segments_observed"] == [
        "sh_main",
        "sh_star",
        "sz_chinext",
        "sz_main",
    ]
    assert dimension["baostock_observed_isST"] == ["0", "1"]
    assert dimension["zero_volume_zero_amount_row_counts"] == {
        "A": 1,
        "B": 1,
        "C": 1,
        "D": 1,
    }
    assert dimension["low_liquidity_row_counts_le_100_shares"] == {
        "A": 2,
        "B": 2,
        "C": 2,
        "D": 2,
    }
    encoded = json.dumps(summary, separators=(",", ":"), sort_keys=True)
    assert "provider_payload" not in encoded
    assert summary["representative_evidence_sufficient"] is False
    assert summary["threshold_candidate_active"] is False


def test_representative_dry_run_script_outputs_only_canonical_json_without_vendors() -> None:
    script = Path(__file__).parents[2] / "scripts" / "probe_s1_07k3_representative.py"
    bootstrap = (
        "import json, runpy, sys; "
        f"sys.argv = [{str(script)!r}, '--dry-run']; "
        f"module = runpy.run_path({str(script)!r}, run_name='k3_representative_dry_run'); "
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
    assert payload["marker"] == K3_REPRESENTATIVE_PROBE_MARKER
    assert payload["network_calls_made"] == 0
    assert json.loads(lines[1]) == {"vendors_in_modules": []}
    assert completed.stderr == ""
