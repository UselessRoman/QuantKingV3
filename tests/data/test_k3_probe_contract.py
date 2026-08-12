from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from tests.data.adapters.test_akshare_adapter import FakeModule, _rows

from quantking.common.time import TradingDate, UtcTimestamp
from quantking.data.adapters.sources.akshare import (
    AkshareDailyBarAdapter,
    AkshareMarketDataGateway,
)
from quantking.data.adapters.sources.normalization import normalize_market_data_response
from quantking.data.comparison.k3_probe import (
    K3_PROBE_MAX_INVOCATIONS,
    K3ProbeSourceResult,
    fixed_k3_probe_plan,
    k3_probe_dry_run_payload,
    summarize_k3_probe,
)
from quantking.data.incremental import FetchChunk
from quantking.data.models import DataProvider, DatasetKind, DataUpstream


def _record(*, close: float, amount: float = 1000.0) -> dict[str, object]:
    return {
        "code": "600000.SH",
        "trade_date": date(2026, 8, 7),
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": close,
        "pre_close": 10.0,
        "volume": 100,
        "amount": amount,
    }


def _source(
    role: str,
    provider: DataProvider,
    upstream: DataUpstream,
    record: dict[str, object],
    *,
    volume_units: tuple[str, ...] = ("share",),
) -> K3ProbeSourceResult:
    return K3ProbeSourceResult(
        role=role,
        provider=provider,
        upstream=upstream,
        library_versions=("0.9.3",) if role == "A" else ("1.18.83",),
        contract_versions=(
            ("baostock-daily-bar.v1",) if role == "A" else ("akshare-daily-bar.v1",)
        ),
        volume_units=volume_units,
        amount_units=("cny_yuan",),
        unadjusted=True,
        complete=True,
        upstream_row_count=1,
        records=(record,),
        official_preclose=(
            {("600000.SH", "2026-08-07"): Decimal("99.99")} if role == "A" else None
        ),
    )


def _clock() -> UtcTimestamp:
    return UtcTimestamp(datetime(2026, 8, 8, tzinfo=UTC))


def _chunk() -> FetchChunk:
    return FetchChunk(
        sequence=0,
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        scope=("600000.SH",),
        start=TradingDate(date(2026, 8, 1)),
        end=TradingDate(date(2026, 8, 8)),
        request_sha256="a" * 64,
    )


def test_dry_run_freezes_eight_endpoints_and_distinct_protocol_http_budgets() -> None:
    payload = dict(k3_probe_dry_run_payload())
    assert payload["network_calls_made"] == 0
    assert payload["application_retries"] == 0
    assert payload["market_data_endpoint_invocations"] == {
        "BAOSTOCK": 2,
        "EASTMONEY": 2,
        "TENCENT": 2,
        "SINA": 2,
        "total": 8,
    }
    assert payload["baostock_protocol_message_upper_bound"] == {
        "login": 2,
        "query": 2,
        "logout_attempt": 2,
        "total": 6,
    }
    assert payload["akshare_locked_source_http_upper_bound"] == {
        "EASTMONEY": 2,
        "TENCENT": 6,
        "SINA": 4,
        "total": 12,
    }
    assert payload["failure_policy"] == "STOP_ON_FIRST_FAILURE"
    assert payload["writes_formal_data_root"] is False
    assert payload["threshold_candidate_active"] is False
    plan = fixed_k3_probe_plan()
    assert len(plan) == K3_PROBE_MAX_INVOCATIONS == 8
    assert {item.symbol for item in plan} == {"000001.SZ", "600000.SH"}
    assert {item.role for item in plan} == {"A", "B", "C", "D"}
    assert all((item.target_start - item.upstream_start).days == 15 for item in plan)


def test_dry_run_script_outputs_only_canonical_json_and_does_not_import_vendors() -> None:
    script = Path(__file__).parents[2] / "scripts" / "probe_s1_07k3_four_sources.py"
    bootstrap = (
        "import json, runpy, sys; "
        f"sys.argv = [{str(script)!r}, '--dry-run']; "
        f"module = runpy.run_path({str(script)!r}, run_name='k3_probe_dry_run'); "
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
    assert payload["network_calls_made"] == 0
    assert json.loads(lines[1]) == {"vendors_in_modules": []}
    assert completed.stderr == ""


def test_summary_is_bounded_decimal_evidence_not_provider_payload() -> None:
    results = (
        _source("A", DataProvider.BAOSTOCK, DataUpstream.BAOSTOCK, _record(close=10.5)),
        _source("B", DataProvider.AKSHARE, DataUpstream.EASTMONEY, _record(close=10.6)),
        _source("C", DataProvider.AKSHARE, DataUpstream.TENCENT, _record(close=10.5)),
        _source("D", DataProvider.AKSHARE, DataUpstream.SINA, _record(close=10.5)),
    )
    summary = dict(summarize_k3_probe(results))
    close = summary["pairs"]["A-B"]["fields"]["close"]
    assert close == {
        "max_absolute_difference": "0.1",
        "max_key": ["600000.SH", "2026-08-07"],
        "zero_difference_count": 0,
        "nonzero_difference_count": 1,
    }
    assert summary["pairs"]["A-C"]["fields"]["close"] == {
        "max_absolute_difference": "0",
        "max_key": None,
        "zero_difference_count": 1,
        "nonzero_difference_count": 0,
    }
    assert summary["baostock_official_preclose_semantics"] == {
        "compared_count": 1,
        "different_from_derived_count": 1,
        "max_absolute_difference": "89.99",
        "official_preclose_used_for_canonical": False,
    }
    encoded = json.dumps(summary, separators=(",", ":"), sort_keys=True)
    assert "provider_payload" not in encoded
    assert "99.99" not in encoded
    assert summary["representative_evidence_sufficient"] is False
    assert summary["threshold_candidate_active"] is False


def test_probe_compares_canonical_volume_after_lot_to_share_normalization() -> None:
    eastmoney_rows = _rows(DataUpstream.EASTMONEY)
    eastmoney_rows[0]["成交量"] = 1
    tencent_rows = _rows(DataUpstream.TENCENT)
    tencent_rows[0]["volume"] = 100
    eastmoney = AkshareMarketDataGateway(
        AkshareDailyBarAdapter(
            DataUpstream.EASTMONEY,
            module_loader=lambda: FakeModule(eastmoney_rows),
            clock=_clock,
        )
    ).fetch(_chunk())
    tencent = AkshareMarketDataGateway(
        AkshareDailyBarAdapter(
            DataUpstream.TENCENT,
            module_loader=lambda: FakeModule(tencent_rows),
            clock=_clock,
        )
    ).fetch(_chunk())
    eastmoney_records = tuple(
        normalize_market_data_response(DatasetKind.DAILY_BAR_UNADJUSTED, eastmoney).to_pylist()
    )
    tencent_records = tuple(
        normalize_market_data_response(DatasetKind.DAILY_BAR_UNADJUSTED, tencent).to_pylist()
    )
    assert eastmoney.contract.volume_unit.value == "lot_100_shares"
    assert tencent.contract.volume_unit.value == "share"
    assert eastmoney_records[0]["volume"] == tencent_records[0]["volume"] == 100
    base = dict(eastmoney_records[0])
    results = (
        _source("A", DataProvider.BAOSTOCK, DataUpstream.BAOSTOCK, base),
        _source(
            "B",
            DataProvider.AKSHARE,
            DataUpstream.EASTMONEY,
            dict(eastmoney_records[0]),
            volume_units=(eastmoney.contract.volume_unit.value,),
        ),
        _source(
            "C",
            DataProvider.AKSHARE,
            DataUpstream.TENCENT,
            dict(tencent_records[0]),
            volume_units=(tencent.contract.volume_unit.value,),
        ),
        _source("D", DataProvider.AKSHARE, DataUpstream.SINA, dict(base)),
    )
    summary = summarize_k3_probe(results)
    volume = summary["pairs"]["B-C"]["fields"]["volume"]
    assert volume["max_absolute_difference"] == "0"
    assert volume["zero_difference_count"] == 1


def test_probe_rejects_non_numeric_canonical_evidence() -> None:
    invalid = deepcopy(_record(close=10.5))
    invalid["volume"] = "100"
    results = (
        _source("A", DataProvider.BAOSTOCK, DataUpstream.BAOSTOCK, invalid),
        _source("B", DataProvider.AKSHARE, DataUpstream.EASTMONEY, _record(close=10.5)),
        _source("C", DataProvider.AKSHARE, DataUpstream.TENCENT, _record(close=10.5)),
        _source("D", DataProvider.AKSHARE, DataUpstream.SINA, _record(close=10.5)),
    )
    with pytest.raises(TypeError, match="Canonical volume must be numeric"):
        summarize_k3_probe(results)
