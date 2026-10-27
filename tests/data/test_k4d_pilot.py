"""S1-07K4D pilot offline contract, executor, and zero-I/O dry-run tests."""

from __future__ import annotations

import io
import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quantking.cli.legacy_import import run_cli
from quantking.data.adapters.canonical_schema import canonical_schema, ensure_sorted
from quantking.data.adapters.legacy_import import LegacyInventoryPlanner
from quantking.data.adapters.legacy_pilot import (
    K4DServiceFetchResult,
    LegacyPilotError,
    LegacyPilotExecutor,
    PilotInvocationJournal,
    transform_k4d_tables,
    verify_legacy_pilot_batch,
)
from quantking.data.adapters.legacy_recheck import LegacyRecheckEngine
from quantking.data.adapters.sources.contracts import (
    NetworkReason,
    SourceAdapterError,
    SourceErrorCode,
)
from quantking.data.comparison.thresholds import (
    K4_THRESHOLD_RULE_VERSION,
    thresholds_for,
)
from quantking.data.legacy_import.contracts import (
    K4D_INVENTORY_SYNTHETIC_MARKER,
    K4D_PILOT_REAL_MARKER,
    K4D_PILOT_SYNTHETIC_MARKER,
    LegacyImportAuthorization,
    LegacyImportMode,
    LegacyImportSemantics,
    LegacyInventoryEntry,
    canonical_json_bytes,
    sha256_bytes,
)
from quantking.data.models import DatasetKind, DataUpstream
from quantking.history.stage1.k4d_pilot import (
    K4D_EXPECTED_CANONICAL_ROW_COUNT,
    K4D_EXPECTED_FIRST_ROW_EXCLUSIONS,
    K4D_PILOT_SYMBOLS,
    K4D_RECHECK_WINDOW_END,
    K4D_RECHECK_WINDOW_START,
    K4DSemanticsError,
    K4DSemanticsStatus,
    LegacyK4DSemantics,
    build_k4d_pilot_plan,
    classify_k4d_row,
    confirm_k4d_units,
    k4d_hypothesis_payload,
    parse_k4d_date,
    prove_k4d_unadjusted,
    select_k4d_pilot_entries,
)

_ROOT = Path(__file__).resolve().parents[2]
_FROZEN_PLAN = _ROOT / "docs/stage1/evidence/k4d_pilot_dry_run_2026-08-16.json"
_FAILED_ATTEMPT = _ROOT / "docs/stage1/evidence/k4d-pilot-attempts/K4D-PILOT-30fb6c198a0e"
_FAILED_ATTEMPT_2 = _ROOT / "docs/stage1/evidence/k4d-pilot-attempts/K4D-PILOT-d0280d367480"
_ATTEMPT_2_DIAGNOSTIC = _ROOT / "docs/stage1/evidence/k4d_pilot_attempt2_diagnostic_2026-08-17.json"
_ATTEMPT_2_DIAGNOSTIC_SHA256 = "b75f27b4096571f04eb790e8cd78f1da1faf174187dc44ab0ca09d20fe1c02e1"
_FROZEN_PLAN_SHA256 = "c77b2d8e109621bc79be390a59c3bccaa81b8411638f6fcd3e8378db00875fb1"
_FROZEN_PLAN_PLAN_SHA256 = "76e928aa157dc18e23cbe4876a6613bfda019f79d72f195c331a428418727737"
_FROZEN_INVENTORY_SHA256 = "140978ea3e3f4d9139c15d641c92564af8f58928bf77017117393ce988a04ce2"
_ROW_COUNTS = {
    "000001.SZ": 8_641,
    "000858.SZ": 6_814,
    "300750.SZ": 1_935,
    "600000.SH": 6_435,
    "688237.SH": 950,
    "688981.SH": 1_425,
}
_START = date(2015, 1, 1)
_WINDOW_START = date.fromisoformat(K4D_RECHECK_WINDOW_START)
_WINDOW_END = date.fromisoformat(K4D_RECHECK_WINDOW_END)
_K4 = thresholds_for(K4_THRESHOLD_RULE_VERSION)
_SOURCE_SCHEMA = pa.schema(
    [
        ("code", pa.string()),
        ("date", pa.string()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),
        ("amount", pa.float64()),
    ]
)


def _source_row(code: str, day: date, close: float, *, volume: float = 1000.0) -> dict[str, object]:
    return {
        "code": code,
        "date": day.strftime("%Y%m%d"),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
        "amount": close * volume,
    }


def _fixture_rows(code: str, count: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    close = 10.0
    for index in range(count):
        day = _START + timedelta(days=index)
        if code == "600000.SH" and index == 100:
            rows.append(_source_row(code, day, close, volume=0.0))
            rows[-1]["amount"] = 0.0
            continue
        close = 10.0 + (index % 50) * 0.2
        rows.append(_source_row(code, day, close, volume=1000.0 + index))
    return rows


def _write_source(source_root: Path) -> None:
    for code, count in _ROW_COUNTS.items():
        table = pa.Table.from_pylist(_fixture_rows(code, count), schema=_SOURCE_SCHEMA)
        pq.write_table(table, source_root / f"{code}.parquet")


def _inventory_payload(source_root: Path) -> dict[str, object]:
    authorization = LegacyImportAuthorization(
        source_root=source_root,
        staging_root=source_root.parent / "inv-staging",
        mode=LegacyImportMode.INVENTORY,
        marker=K4D_INVENTORY_SYNTHETIC_MARKER,
    )
    planner = LegacyInventoryPlanner(authorization)
    return planner.inventory(planner.discover()).to_payload()


def _canonical_table(rows: list[dict[str, object]]) -> pa.Table:
    typed = [
        {
            **row,
            "trade_date": date.fromisoformat(str(row["trade_date"])),
            "volume": round(float(row["volume"])),
        }
        for row in rows
    ]
    return ensure_sorted(
        DatasetKind.DAILY_BAR_UNADJUSTED.value,
        pa.Table.from_pylist(
            typed, schema=canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value)
        ),
    )


def _entry(code: str, *, size: int = 10, rows: int = 1) -> LegacyInventoryEntry:
    return LegacyInventoryEntry(
        relative_path=f"{code}.parquet",
        size=size,
        mtime_ns=1,
        sha256="a" * 64,
        parquet_schema="code: string",
        row_count=rows,
        code_min=code,
        code_max=code,
        date_min="20200101",
        date_max="20260603",
        null_counts={},
        duplicate_key_count=0,
        read_error=None,
    )


def _x_row(
    code: str,
    day: str,
    close: float,
    *,
    pre_close: float | None = None,
    volume: float = 1000.0,
) -> dict[str, object]:
    return {
        "code": code,
        "trade_date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "pre_close": close if pre_close is None else pre_close,
        "volume": volume,
        "amount": close * volume,
    }


# 1/2/3/4/5/6: real-shape schema, strict dates, derivation, first row, suspension.


def test_parse_k4d_date_accepts_only_strict_eight_digit_dates() -> None:
    assert parse_k4d_date("20260603") == date(2026, 6, 3)
    assert parse_k4d_date("19910107") == date(1991, 1, 7)
    for bad in ("2026-6-3", "2026063", "202606033", "abcdefgh", "20260230", "", None, 20260603):
        with pytest.raises(K4DSemanticsError, match="INVALID_DATE_FORMAT"):
            parse_k4d_date(bad)


def test_transform_real_shape_schema_derives_pre_close_and_excludes_first_rows() -> None:
    table = pa.Table.from_pylist(
        [
            _source_row("600000.SH", date(2026, 5, 30), 10.0),
            _source_row("600000.SH", date(2026, 6, 1), 11.0),
            _source_row("600000.SH", date(2026, 6, 2), 12.0),
        ],
        schema=_SOURCE_SCHEMA,
    )
    transform = transform_k4d_tables([table])
    assert transform.raw_row_count == 3
    assert transform.first_row_excluded_count == 1
    assert transform.isolated_row_count == 0
    rows = transform.canonical.to_pylist()
    assert [row["trade_date"] for row in rows] == [date(2026, 6, 1), date(2026, 6, 2)]
    assert rows[0]["pre_close"] == 10.0
    assert rows[1]["pre_close"] == 11.0
    assert ("600000.SH", "2026-05-30") not in transform.x_rows
    assert transform.raw_row_count == (
        transform.canonical.num_rows
        + transform.first_row_excluded_count
        + transform.isolated_row_count
    )


def test_transform_isolates_bad_dates_nulls_and_keeps_conservation() -> None:
    rows = [
        _source_row("600000.SH", date(2026, 6, 1), 10.0),
        {**_source_row("600000.SH", date(2026, 6, 2), 11.0), "date": "2026-6-2"},
        {**_source_row("600000.SH", date(2026, 6, 3), 12.0), "volume": None},
    ]
    transform = transform_k4d_tables([pa.Table.from_pylist(rows, schema=_SOURCE_SCHEMA)])
    assert transform.isolated_row_count == 2
    assert transform.isolated_reasons == (
        "ISOLATED:INVALID_DATE_FORMAT",
        "ISOLATED:REQUIRED_FIELD_NULL",
    )
    assert transform.canonical.num_rows == 0
    assert transform.first_row_excluded_count == 1


def test_suspension_placeholder_classification_success_and_reverse_isolation() -> None:
    placeholder = _source_row("600000.SH", date(2026, 6, 2), 10.0, volume=0.0)
    placeholder["amount"] = 0.0
    assert classify_k4d_row(placeholder, 10.0) == "SUSPENDED_PLACEHOLDER"
    moved = dict(placeholder)
    moved["close"] = 10.5
    assert classify_k4d_row(moved, 10.0) == "ISOLATED:ZERO_VOLUME_PRICE_MOVEMENT"
    partial = dict(placeholder)
    partial["amount"] = 1.0
    assert classify_k4d_row(partial, 10.0) == "ISOLATED:PARTIAL_ZERO_VOLUME_AMOUNT"
    negative = dict(placeholder)
    negative["volume"] = 5.0
    negative["amount"] = -1.0
    assert classify_k4d_row(negative, 10.0) == "ISOLATED:NEGATIVE_VALUE:amount"


def test_transform_keeps_suspension_placeholder_and_missing_days_absent() -> None:
    rows = [
        _source_row("600000.SH", date(2026, 6, 1), 10.0),
        _source_row("600000.SH", date(2026, 6, 2), 10.0, volume=0.0),
        {**_source_row("600000.SH", date(2026, 6, 2), 10.0, volume=0.0), "amount": 0.0},
        _source_row("600000.SH", date(2026, 6, 5), 11.0),
    ]
    rows[2]["amount"] = 0.0
    transform = transform_k4d_tables([pa.Table.from_pylist(rows, schema=_SOURCE_SCHEMA)])
    assert transform.suspended_placeholder_count == 1
    keys = {key for key in transform.x_rows}
    assert keys == {("600000.SH", "2026-06-02"), ("600000.SH", "2026-06-05")}
    assert ("600000.SH", "2026-06-03") not in keys
    assert ("600000.SH", "2026-06-04") not in keys


# 7/8/9: unit-scale and unadjusted confirmation rules.


def _service_index(
    rows: list[dict[str, object]],
) -> dict[str, dict[tuple[str, str], dict[str, object]]]:
    return {"BAOSTOCK": {(str(row["code"]), str(row["trade_date"])): dict(row) for row in rows}}


def test_unit_factor_one_is_uniquely_confirmed() -> None:
    x = {
        ("600000.SH", f"2026-06-{day:02d}"): _x_row("600000.SH", f"2026-06-{day:02d}", 10.0 + day)
        for day in range(1, 26)
    }
    rows = list(x.values())
    ok, reasons = confirm_k4d_units(
        x_rows=x, service_rows=_service_index(rows), thresholds=_K4, min_sample=20
    )
    assert ok and reasons == ()


def test_unit_factor_hundred_and_ambiguity_and_small_sample_fail_stably() -> None:
    x = {
        ("600000.SH", f"2026-06-{day:02d}"): _x_row("600000.SH", f"2026-06-{day:02d}", 10.0 + day)
        for day in range(1, 26)
    }
    scaled = [
        {**row, "volume": row["volume"] * 100, "amount": row["amount"] * 1000} for row in x.values()
    ]
    ok, reasons = confirm_k4d_units(
        x_rows=x, service_rows=_service_index(scaled), thresholds=_K4, min_sample=20
    )
    assert not ok
    assert "UNIT_SCALE_FACTOR_ONE_NOT_SATISFIED:volume" in reasons
    assert "UNIT_SCALE_FACTOR_ONE_NOT_SATISFIED:amount" in reasons
    zero_rows = [
        _x_row("600000.SH", f"2026-06-{day:02d}", 0.0, pre_close=0.0, volume=0.0)
        for day in range(1, 26)
    ]
    for row in zero_rows:
        row["open"] = row["high"] = row["low"] = row["close"] = 0.0
        row["amount"] = 0.0
    ok, reasons = confirm_k4d_units(
        x_rows={("600000.SH", str(row["trade_date"])): row for row in zero_rows},
        service_rows=_service_index(zero_rows),
        thresholds=_K4,
        min_sample=20,
    )
    assert not ok
    assert any(reason.startswith("UNIT_SCALE_AMBIGUOUS:volume") for reason in reasons)
    ok, reasons = confirm_k4d_units(
        x_rows=x, service_rows=_service_index(list(x.values())), thresholds=_K4, min_sample=50
    )
    assert not ok
    assert any(reason.startswith("UNIT_SAMPLE_INSUFFICIENT") for reason in reasons)
    with pytest.raises(K4DSemanticsError):
        confirm_k4d_units(
            x_rows=x,
            service_rows=_service_index(list(x.values())),
            thresholds=thresholds_for("comparison-thresholds.daily-bar-unadjusted.k1.v1"),
        )


def test_unadjusted_proof_requires_a_material_vendor_boundary() -> None:
    x = {("600000.SH", "2026-06-02"): _x_row("600000.SH", "2026-06-02", 12.0, pre_close=11.0)}
    service = {("600000.SH", "2026-06-02"): dict(x[("600000.SH", "2026-06-02")])}
    ok, boundary, reasons = prove_k4d_unadjusted(
        vendor_pre_close={("600000.SH", "2026-06-02"): 10.0},
        service_canonical=service,
        x_rows=x,
        thresholds=_K4,
    )
    assert ok and reasons == ()
    assert boundary is not None
    assert boundary["code"] == "600000.SH"
    assert boundary["vendor_reference_pre_close"] == 10.0
    assert boundary["derived_unadjusted_pre_close"] == 11.0
    ok, _, reasons = prove_k4d_unadjusted(
        vendor_pre_close={("600000.SH", "2026-06-02"): 11.0},
        service_canonical=service,
        x_rows=x,
        thresholds=_K4,
    )
    assert not ok and reasons == ("SEMANTICS_UNADJUSTED_NOT_PROVEN",)
    ok, _, reasons = prove_k4d_unadjusted(
        vendor_pre_close={}, service_canonical=service, x_rows=x, thresholds=_K4
    )
    assert not ok and reasons == ("SEMANTICS_UNADJUSTED_NOT_PROVEN",)


# 10/11: K4 threshold binding and four-state recheck conservation.


def test_recheck_requires_explicit_k4_thresholds_and_tolerates_fifty_only() -> None:
    with pytest.raises(TypeError, match="thresholds"):
        LegacyRecheckEngine()

    def row(volume: float) -> dict[str, object]:
        return {
            "code": "600000.SH",
            "trade_date": "2026-06-01",
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "pre_close": 10.0,
            "volume": volume,
            "amount": 10_000.0,
        }

    x = _canonical_table([row(1000)])
    within = _canonical_table([row(1050)])
    beyond = _canonical_table([row(1051)])
    engine = LegacyRecheckEngine(thresholds=_K4)
    assert (
        engine.reevaluate(
            x_table=x, service_tables={DataUpstream.BAOSTOCK: within}
        ).evidence.x_a_consensus_rows
        == 1
    )
    assert (
        engine.reevaluate(
            x_table=x, service_tables={DataUpstream.BAOSTOCK: beyond}
        ).evidence.quarantined_rows
        == 1
    )


def test_recheck_four_states_conserve_every_row_under_k4() -> None:
    rows = [
        _x_row("600000.SH", "2026-06-01", 10.0),
        _x_row("600000.SH", "2026-06-02", 12.0),
        _x_row("600000.SH", "2026-06-03", 10.9),
        _x_row("600000.SH", "2026-06-04", 10.8),
    ]
    x = _canonical_table(rows)
    a = _canonical_table(
        [
            _x_row("600000.SH", "2026-06-01", 10.0),
            _x_row("600000.SH", "2026-06-02", 11.0),
            _x_row("600000.SH", "2026-06-03", 10.5),
        ]
    )
    b = _canonical_table([_x_row("600000.SH", "2026-06-02", 11.0)])
    outcome = LegacyRecheckEngine(thresholds=_K4).reevaluate(
        x_table=x,
        service_tables={DataUpstream.BAOSTOCK: a, DataUpstream.TENCENT: b},
    )
    evidence = outcome.evidence
    assert evidence.input_rows == 4
    assert evidence.x_a_consensus_rows == 1
    assert evidence.service_consensus_replaced_x_rows == 1
    assert evidence.quarantined_rows == 1
    assert evidence.x_only_unverified_rows == 1
    assert (
        evidence.x_a_consensus_rows
        + evidence.service_consensus_replaced_x_rows
        + evidence.quarantined_rows
        + evidence.x_only_unverified_rows
        == evidence.input_rows
    )


# 12: exact six-security selection.


def test_selection_is_exact_match_and_rejects_fuzzy_partial_or_extra() -> None:
    entries = tuple(_entry(code) for code in K4D_PILOT_SYMBOLS)
    assert [e.code_min for e in select_k4d_pilot_entries(entries)] == list(K4D_PILOT_SYMBOLS)
    with pytest.raises(K4DSemanticsError, match=r"PILOT_SYMBOL_MISSING:000858\.SZ"):
        select_k4d_pilot_entries(tuple(e for e in entries if e.code_min != "000858.SZ"))
    with pytest.raises(K4DSemanticsError, match=r"PILOT_SYMBOL_AMBIGUOUS:600000\.SH"):
        select_k4d_pilot_entries((*entries, _entry("600000.SH")))
    ranged = (
        *entries[:4],
        LegacyInventoryEntry(
            relative_path="range.parquet",
            size=10,
            mtime_ns=1,
            sha256="b" * 64,
            parquet_schema="code: string",
            row_count=1,
            code_min="600000.SH",
            code_max="688981.SH",
            date_min="20200101",
            date_max="20260603",
            null_counts={},
            duplicate_key_count=0,
            read_error=None,
        ),
    )
    with pytest.raises(K4DSemanticsError, match=r"PILOT_SYMBOL_MISSING:688237\.SH"):
        select_k4d_pilot_entries(ranged)


# 17 (part one): the frozen dry-run plan bytes and identity.


def test_frozen_pilot_plan_is_byte_stable_and_hypothesis_only() -> None:
    raw = _FROZEN_PLAN.read_bytes()
    payload = json.loads(raw)
    assert canonical_json_bytes(payload) + b"\n" == raw
    assert sha256_bytes(raw) == _FROZEN_PLAN_SHA256
    unsigned = {k: v for k, v in payload.items() if k != "plan_sha256"}
    assert payload["plan_sha256"] == sha256_bytes(canonical_json_bytes(unsigned))
    assert payload["plan_sha256"] == _FROZEN_PLAN_PLAN_SHA256
    assert payload["authorization_marker"] == "S1-07K4D-PILOT-REAL-MIGRATION-V1"
    assert payload["inventory_sha256"] == _FROZEN_INVENTORY_SHA256
    assert payload["totals"] == {"file_count": 6, "byte_count": 878_893, "raw_row_count": 26_200}
    assert payload["expected_canonical_row_count"] == K4D_EXPECTED_CANONICAL_ROW_COUNT == 26_194
    assert payload["expected_first_row_exclusions"] == K4D_EXPECTED_FIRST_ROW_EXCLUSIONS == 6
    assert payload["semantics"]["status"] == K4DSemanticsStatus.HYPOTHESIS.value
    assert payload["reads_legacy_root"] is False
    assert payload["calls_service_network"] is False
    assert payload["writes_staging"] is False
    assert payload["accesses_backup_root"] is False
    assert payload["publishes_warehouse"] is False
    assert payload["requires_new_authorization_for_execution"] is True
    assert payload["k4_threshold_rule_version"] == K4_THRESHOLD_RULE_VERSION
    assert [f["code"] for f in payload["pilot_files"]] == list(K4D_PILOT_SYMBOLS)
    assert payload["pilot_files"][0]["sha256"].startswith("b8800b51ab3a8f80")
    semantics = LegacyK4DSemantics(K4DSemanticsStatus.HYPOTHESIS)
    assert payload["semantics"] == semantics.to_payload() == k4d_hypothesis_payload()


# 13: the CLI dry-run performs zero external I/O under the real marker.


def test_pilot_dry_run_cli_has_zero_legacy_root_network_and_staging_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging_root = tmp_path / "staging-root"
    plan_file = tmp_path / "plan.json"

    def boom(*args: object, **kwargs: object) -> Any:
        raise AssertionError("pilot dry-run must not walk, read parquet, or open sockets")

    monkeypatch.setattr(os, "walk", boom)
    monkeypatch.setattr(pq, "read_table", boom)
    import socket

    monkeypatch.setattr(socket, "create_connection", boom)
    stream = io.StringIO()
    code = run_cli(
        [
            "pilot",
            "--dry-run",
            "--inventory-file",
            "docs/stage1/evidence/k4d_inventory_2026-08-16.json",
            "--plan-file",
            str(plan_file),
            "--marker",
            "real",
        ],
        stdout=stream,
    )
    assert code == 0
    payload = json.loads(stream.getvalue())
    assert payload["authorization_marker"] == K4D_PILOT_REAL_MARKER
    assert payload["reads_legacy_root"] is False
    assert payload["calls_service_network"] is False
    assert payload["writes_staging"] is False
    assert payload["accesses_backup_root"] is False
    assert payload["plan_sha256"] == _FROZEN_PLAN_PLAN_SHA256
    assert plan_file.read_bytes() == canonical_json_bytes(payload) + b"\n"
    assert not staging_root.exists()
    assert list(tmp_path.iterdir()) == [plan_file]


def test_pilot_cli_rejects_execution_without_dry_run() -> None:
    stream = io.StringIO()
    code = run_cli(
        [
            "pilot",
            "--inventory-file",
            "x",
            "--plan-file",
            "y",
            "--marker",
            "real",
        ],
        stdout=stream,
    )
    assert code == 2
    assert json.loads(stream.getvalue())["error"]["code"] == "INVALID_ARGUMENT"


def test_pilot_cli_regenerates_the_frozen_plan_byte_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_ROOT)
    plan_file = tmp_path / "regen.json"
    stream = io.StringIO()
    code = run_cli(
        [
            "pilot",
            "--dry-run",
            "--inventory-file",
            "docs/stage1/evidence/k4d_inventory_2026-08-16.json",
            "--plan-file",
            str(plan_file),
            "--marker",
            "real",
        ],
        stdout=stream,
    )
    assert code == 0
    assert plan_file.read_bytes() == _FROZEN_PLAN.read_bytes()


# 14/15: synthetic end-to-end pilot and fail-closed paths.


class _PilotFixture:
    def __init__(self, tmp_path: Path) -> None:
        self.source_root = tmp_path / "source"
        self.staging_root = tmp_path / "staging"
        self.source_root.mkdir()
        _write_source(self.source_root)
        self.inventory_payload = _inventory_payload(self.source_root)
        inventory_file = tmp_path / "inventory.json"
        inventory_file.write_bytes(canonical_json_bytes(self.inventory_payload) + b"\n")
        self.plan = build_k4d_pilot_plan(
            inventory_payload=self.inventory_payload,
            inventory_sha256=sha256_bytes(inventory_file.read_bytes()),
            marker=K4D_PILOT_SYNTHETIC_MARKER,
            inventory_relative_path="inventory.json",
        )
        tables = [pq.read_table(self.source_root / f"{code}.parquet") for code in _ROW_COUNTS]
        self.expected = transform_k4d_tables(tables)
        assert self.expected.canonical.num_rows == K4D_EXPECTED_CANONICAL_ROW_COUNT
        window_rows = [
            row
            for row in self.expected.canonical.to_pylist()
            if _WINDOW_START <= row["trade_date"] <= _WINDOW_END
        ]
        self.window_count = len(window_rows)
        self.window_tables = {
            code: _canonical_table([row for row in window_rows if row["code"] == code])
            for code in _ROW_COUNTS
        }
        first_window_row = next(row for row in window_rows if row["code"] == "000001.SZ")
        boundary_date = first_window_row["trade_date"].isoformat()
        self.boundary_key = ("000001.SZ", boundary_date)
        self.boundary_vendor_value = float(first_window_row["pre_close"]) * 0.9

    def fetcher(self, symbol: str, _journal: PilotInvocationJournal) -> K4DServiceFetchResult:
        table = self.window_tables[symbol]
        vendor: dict[tuple[str, str], float] = {}
        if symbol == "000001.SZ":
            vendor[self.boundary_key] = self.boundary_vendor_value
        return K4DServiceFetchResult(
            symbol=symbol,
            canonical={
                DataUpstream.BAOSTOCK: table,
                DataUpstream.TENCENT: table,
                DataUpstream.SINA: table,
            },
            vendor_pre_close=vendor,
            evidence={
                "row_counts": {
                    upstream.value: table.num_rows
                    for upstream in (
                        DataUpstream.BAOSTOCK,
                        DataUpstream.TENCENT,
                        DataUpstream.SINA,
                    )
                }
            },
        )

    def authorization(self) -> LegacyImportAuthorization:
        return LegacyImportAuthorization(
            source_root=self.source_root,
            staging_root=self.staging_root,
            mode=LegacyImportMode.PILOT,
            marker=K4D_PILOT_SYNTHETIC_MARKER,
            pilot_symbols=K4D_PILOT_SYMBOLS,
        )

    def executor(self, fetcher: Any) -> LegacyPilotExecutor:
        return LegacyPilotExecutor(
            authorization=self.authorization(),
            plan=self.plan,
            inventory_payload=self.inventory_payload,
            fetcher=fetcher,
        )


def _journal_lines(path: Path) -> list[dict[str, object]]:
    """Parse one invocation journal, asserting every line is canonical JSON."""
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    records: list[dict[str, object]] = []
    for line in raw.split(b"\n")[:-1]:
        record = json.loads(line)
        assert isinstance(record, dict)
        assert canonical_json_bytes(record) == line
        records.append(record)
    return records


def test_synthetic_end_to_end_pilot_stages_rechecks_and_never_publishes(
    tmp_path: Path,
) -> None:
    fixture = _PilotFixture(tmp_path)
    executor = fixture.executor(fixture.fetcher)
    outcome = executor.run(
        authorization_id="K4D-PILOT-SYNTHETIC-1",
        attempt_dir=tmp_path / "attempts" / "a1",
        idempotency_key="k4d-pilot-synthetic-1",
    )
    assert outcome.canonical_row_count == 26_194
    assert outcome.raw_row_count == 26_200
    assert outcome.confirmation.status is K4DSemanticsStatus.CONFIRMED
    assert outcome.confirmation.first_row_excluded_count == 6
    assert outcome.confirmation.isolated_row_count == 0
    assert outcome.confirmation.unadjusted_boundary is not None
    assert outcome.confirmation.unadjusted_boundary["code"] == "000001.SZ"
    evidence = outcome.recheck.evidence
    assert evidence.input_rows == 26_194
    assert evidence.x_a_consensus_rows == fixture.window_count
    assert evidence.x_only_unverified_rows == 26_194 - fixture.window_count
    assert evidence.quarantined_rows == 0
    assert evidence.service_consensus_replaced_x_rows == 0
    assert (
        evidence.x_a_consensus_rows
        + evidence.service_consensus_replaced_x_rows
        + evidence.quarantined_rows
        + evidence.x_only_unverified_rows
        == 26_194
    )
    manifest, manifest_sha = verify_legacy_pilot_batch(
        outcome.directory, staging_root=fixture.staging_root
    )
    assert manifest_sha == outcome.manifest_sha256
    assert manifest["warehouse_published"] is False
    assert manifest["status"] == "PILOT_STAGED_UNPUBLISHED"
    assert manifest["counts"]["suspended_placeholder_count"] == 1
    assert manifest["counts"]["first_row_excluded_count"] == 6
    assert not (fixture.staging_root / "warehouse").exists()
    assert list((fixture.staging_root / "legacy-pilot-idempotency").iterdir())
    assert (tmp_path / "attempts" / "a1" / "attempt-header.json").is_file()
    assert (tmp_path / "attempts" / "a1" / "attempt-header.json.status").is_file()
    journal_records = _journal_lines(tmp_path / "attempts" / "a1" / "invocations.jsonl")
    assert len(journal_records) == 6
    assert [record["sequence"] for record in journal_records] == [1, 2, 3, 4, 5, 6]
    assert all(record["scope"] == "SYMBOL" for record in journal_records)
    assert all(record["status"] == "SUCCESS" for record in journal_records)
    assert [record["symbol"] for record in journal_records] == list(K4D_PILOT_SYMBOLS)
    with pytest.raises(LegacyPilotError, match="PILOT_ATTEMPT_ALREADY_STARTED"):
        executor.run(
            authorization_id="K4D-PILOT-SYNTHETIC-1",
            attempt_dir=tmp_path / "attempts" / "a1",
            idempotency_key="k4d-pilot-synthetic-2",
        )
    with pytest.raises(LegacyPilotError, match="PILOT_IDEMPOTENCY_KEY_USED"):
        fixture.executor(fixture.fetcher).run(
            authorization_id="K4D-PILOT-SYNTHETIC-1B",
            attempt_dir=tmp_path / "attempts" / "a2",
            idempotency_key="k4d-pilot-synthetic-1",
        )


def test_pilot_fails_closed_when_a_service_call_fails(tmp_path: Path) -> None:
    fixture = _PilotFixture(tmp_path)

    def failing_fetcher(symbol: str, _journal: PilotInvocationJournal) -> K4DServiceFetchResult:
        if symbol == "300750.SZ":
            raise RuntimeError("upstream refused")
        return fixture.fetcher(symbol, _journal)

    with pytest.raises(LegacyPilotError, match=r"PILOT_SERVICE_FETCH_FAILED:300750\.SZ"):
        fixture.executor(failing_fetcher).run(
            authorization_id="K4D-PILOT-SYN-FAIL-1",
            attempt_dir=tmp_path / "attempts" / "f1",
            idempotency_key="k4d-pilot-syn-fail-1",
        )
    failure = json.loads(
        (tmp_path / "attempts" / "f1" / "failure.json").read_text(encoding="utf-8")
    )
    assert failure["code"] == "PILOT_SERVICE_FETCH_FAILED:300750.SZ"
    assert not (fixture.staging_root / "legacy-pilot-imports").exists()


def test_pilot_failure_evidence_unpacks_stable_source_error_code(
    tmp_path: Path,
) -> None:
    fixture = _PilotFixture(tmp_path)

    def source_error_fetcher(
        symbol: str, _journal: PilotInvocationJournal
    ) -> K4DServiceFetchResult:
        if symbol == "000858.SZ":
            raise SourceAdapterError(SourceErrorCode.NETWORK, network_reason=NetworkReason.TIMEOUT)
        return fixture.fetcher(symbol, _journal)

    with pytest.raises(LegacyPilotError, match=r"PILOT_SERVICE_FETCH_FAILED:000858\.SZ"):
        fixture.executor(source_error_fetcher).run(
            authorization_id="K4D-PILOT-SYN-FAIL-5",
            attempt_dir=tmp_path / "attempts" / "f5",
            idempotency_key="k4d-pilot-syn-fail-5",
        )
    failure = json.loads(
        (tmp_path / "attempts" / "f5" / "failure.json").read_text(encoding="utf-8")
    )
    assert failure["code"] == "PILOT_SERVICE_FETCH_FAILED:000858.SZ"
    assert failure["symbol"] == "000858.SZ"
    assert failure["source_error"] == {
        "code": "AKSHARE_NETWORK",
        "network_reason": "timeout",
        "failure_kind": "NETWORK_FAILURE",
        "retryable": True,
        "message": "AKShare upstream network request failed",
    }
    records = _journal_lines(tmp_path / "attempts" / "f5" / "invocations.jsonl")
    assert [record["status"] for record in records] == ["SUCCESS", "FAILED"]
    failed = records[1]
    assert failed["scope"] == "SYMBOL"
    assert failed["symbol"] == "000858.SZ"
    assert failed["source_error"] == failure["source_error"]
    assert not (fixture.staging_root / "legacy-pilot-imports").exists()


def test_pilot_journal_records_per_endpoint_calls_with_stable_codes(
    tmp_path: Path,
) -> None:
    fixture = _PilotFixture(tmp_path)

    def endpoint_journaling_fetcher(
        symbol: str, journal: PilotInvocationJournal
    ) -> K4DServiceFetchResult:
        result = fixture.fetcher(symbol, journal)
        for upstream, table in result.canonical.items():
            if symbol == "300750.SZ" and upstream is DataUpstream.SINA:
                journal.record_endpoint_failure(
                    symbol=symbol,
                    provider="AKSHARE",
                    upstream=upstream.value,
                    error=SourceAdapterError(SourceErrorCode.PARSE),
                )
                raise SourceAdapterError(SourceErrorCode.PARSE)
            journal.record_endpoint_success(
                symbol=symbol,
                provider="AKSHARE" if upstream is not DataUpstream.BAOSTOCK else "BAOSTOCK",
                upstream=upstream.value,
                row_count=table.num_rows,
                raw_payload_sha256="0" * 64,
                library_version="synthetic-1.0.0",
            )
        return result

    with pytest.raises(LegacyPilotError, match=r"PILOT_SERVICE_FETCH_FAILED:300750\.SZ"):
        fixture.executor(endpoint_journaling_fetcher).run(
            authorization_id="K4D-PILOT-SYN-FAIL-6",
            attempt_dir=tmp_path / "attempts" / "f6",
            idempotency_key="k4d-pilot-syn-fail-6",
        )
    records = _journal_lines(tmp_path / "attempts" / "f6" / "invocations.jsonl")
    # 000001.SZ and 000858.SZ journal three endpoints each plus the executor's
    # SYMBOL SUCCESS record; 300750.SZ journals two endpoint successes before
    # its SINA endpoint fails, then the executor records the failed symbol.
    assert [record["scope"] for record in records] == (["ENDPOINT"] * 3 + ["SYMBOL"]) * 2 + [
        "ENDPOINT",
        "ENDPOINT",
        "ENDPOINT",
        "SYMBOL",
    ]
    assert [record["sequence"] for record in records] == list(range(1, 13))
    assert [record["status"] for record in records] == ["SUCCESS"] * 10 + [
        "FAILED",
        "FAILED",
    ]
    failed_endpoint = records[10]
    assert failed_endpoint["symbol"] == "300750.SZ"
    assert failed_endpoint["provider"] == "AKSHARE"
    assert failed_endpoint["upstream"] == "SINA"
    assert failed_endpoint["source_error"] == {
        "code": "AKSHARE_PARSE",
        "network_reason": None,
        "failure_kind": "PARSE_FAILURE",
        "retryable": False,
        "message": "AKShare upstream response could not be parsed",
    }
    failed_symbol = records[11]
    assert failed_symbol["status"] == "FAILED"
    assert failed_symbol["symbol"] == "300750.SZ"
    assert failed_symbol["source_error"] == failed_endpoint["source_error"]
    success_endpoint = records[0]
    assert success_endpoint["symbol"] == "000001.SZ"
    assert success_endpoint["provider"] == "BAOSTOCK"
    assert success_endpoint["upstream"] == "BAOSTOCK"
    assert success_endpoint["row_count"] > 0
    assert success_endpoint["raw_payload_sha256"] == "0" * 64
    assert success_endpoint["library_version"] == "synthetic-1.0.0"


def test_pilot_fails_closed_when_semantics_are_not_confirmed(tmp_path: Path) -> None:
    fixture = _PilotFixture(tmp_path)

    def scaled_fetcher(symbol: str, _journal: PilotInvocationJournal) -> K4DServiceFetchResult:
        result = fixture.fetcher(symbol, _journal)
        scaled = {
            upstream: _canonical_table(
                [
                    {**row, "volume": row["volume"] * 100, "amount": row["amount"] * 1000}
                    for row in table.to_pylist()
                ]
            )
            for upstream, table in result.canonical.items()
        }
        return K4DServiceFetchResult(
            symbol=result.symbol,
            canonical=scaled,
            vendor_pre_close={},
            evidence=result.evidence,
        )

    with pytest.raises(LegacyPilotError, match="PILOT_SEMANTICS_NOT_CONFIRMED"):
        fixture.executor(scaled_fetcher).run(
            authorization_id="K4D-PILOT-SYN-FAIL-2",
            attempt_dir=tmp_path / "attempts" / "f2",
            idempotency_key="k4d-pilot-syn-fail-2",
        )
    failure = json.loads(
        (tmp_path / "attempts" / "f2" / "failure.json").read_text(encoding="utf-8")
    )
    assert failure["code"] == "PILOT_SEMANTICS_NOT_CONFIRMED"
    assert not (fixture.staging_root / "legacy-pilot-imports").exists()


def test_pilot_fails_closed_when_source_sha_changes_after_freeze(
    tmp_path: Path,
) -> None:
    fixture = _PilotFixture(tmp_path)
    target = fixture.source_root / "688981.SH.parquet"
    stat_before = target.stat()
    payload = bytearray(target.read_bytes())
    payload[-1] ^= 0xFF
    target.write_bytes(bytes(payload))
    os.utime(target, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    with pytest.raises(LegacyPilotError, match="PILOT_SOURCE_SHA_MISMATCH"):
        fixture.executor(fixture.fetcher).run(
            authorization_id="K4D-PILOT-SYN-FAIL-3",
            attempt_dir=tmp_path / "attempts" / "f3",
            idempotency_key="k4d-pilot-syn-fail-3",
        )


def test_pilot_preserves_failed_batch_when_staging_write_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _PilotFixture(tmp_path)
    import quantking.data.adapters.legacy_pilot as pilot_module

    original = pilot_module._write_parquet
    calls = {"count": 0}

    def flaky(table: pa.Table, path: Path) -> None:
        calls["count"] += 1
        if calls["count"] > 1:
            raise OSError("disk full")
        original(table, path)

    monkeypatch.setattr(pilot_module, "_write_parquet", flaky)
    with pytest.raises(LegacyPilotError, match="PILOT_UNEXPECTED_FAILURE"):
        fixture.executor(fixture.fetcher).run(
            authorization_id="K4D-PILOT-SYN-FAIL-4",
            attempt_dir=tmp_path / "attempts" / "f4",
            idempotency_key="k4d-pilot-syn-fail-4",
        )
    imports_root = fixture.staging_root / "legacy-pilot-imports"
    failed = list(imports_root.glob("*.failed"))
    assert len(failed) == 1
    assert not (failed[0] / "COMPLETE").exists()
    assert not list(imports_root.glob("*.incomplete"))
    assert sorted(path.name for path in imports_root.iterdir()) == [failed[0].name]


def test_pilot_executor_rejects_plan_inventory_or_threshold_tampering(
    tmp_path: Path,
) -> None:
    fixture = _PilotFixture(tmp_path)
    tampered_payload = dict(fixture.plan.payload)
    tampered_payload["source_fingerprint"] = "0" * 64
    from quantking.history.stage1.k4d_pilot import K4DPilotPlan

    tampered = K4DPilotPlan(payload=tampered_payload)
    with pytest.raises(LegacyPilotError, match="PILOT_PLAN_SHA_INVALID"):
        LegacyPilotExecutor(
            authorization=fixture.authorization(),
            plan=tampered,
            inventory_payload=fixture.inventory_payload,
            fetcher=fixture.fetcher,
        )

    k1 = thresholds_for("comparison-thresholds.daily-bar-unadjusted.k1.v1")
    with pytest.raises(LegacyPilotError, match="PILOT_REQUIRES_K4_THRESHOLDS"):
        LegacyPilotExecutor(
            authorization=fixture.authorization(),
            plan=fixture.plan,
            inventory_payload=fixture.inventory_payload,
            fetcher=fixture.fetcher,
            thresholds=k1,
        )
    wrong_symbols = LegacyImportAuthorization(
        source_root=fixture.source_root,
        staging_root=fixture.staging_root,
        mode=LegacyImportMode.PILOT,
        marker=K4D_PILOT_SYNTHETIC_MARKER,
        pilot_symbols=("000001.SZ", "600000.SH", "688981.SH"),
    )
    with pytest.raises(LegacyPilotError, match="PILOT_SYMBOL_SET_INVALID"):
        LegacyPilotExecutor(
            authorization=wrong_symbols,
            plan=fixture.plan,
            inventory_payload=fixture.inventory_payload,
            fetcher=fixture.fetcher,
        )


# 16: K3C historical semantics stay valid alongside the K4D contract.


def test_k3c_semantics_contract_remains_usable_for_historical_payloads() -> None:
    fields = (
        "code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "volume",
        "amount",
    )
    legacy = LegacyImportSemantics(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        schema_version="daily_bar_unadjusted.v1",
        column_mapping={field: field for field in fields},
        unadjusted=True,
        volume_unit="share",
        amount_unit="cny_yuan",
        timezone="Asia/Shanghai",
        suspension_field="suspended",
        suspension_true_values=("1", "True"),
        suspension_semantics="PRICE_EQUALS_PRE_CLOSE_VOLUME_AMOUNT_ZERO",
        null_semantics="REJECT_REQUIRED_FIELD_NULLS",
    )
    assert legacy.confirmed
    assert legacy.to_payload()["suspension_field"] == "suspended"
    hypothesis = LegacyK4DSemantics(K4DSemanticsStatus.HYPOTHESIS).to_payload()
    assert hypothesis["first_row_policy"] == "retain_raw_exclude_from_canonical"
    assert hypothesis["missing_suspension_row_policy"] == "preserve_as_absent_do_not_synthesize"


def test_first_real_pilot_attempt_failure_evidence_is_frozen_as_failed() -> None:
    """The 2026-08-16 first authorized pilot attempt stays a frozen failure.

    The attempt stopped fail-fast on the 6th endpoint call
    (000858.SZ x SINA) with zero staging writes; the evidence must never be
    rewritten into a success and the consumed authorization stays consumed.
    """

    header = json.loads((_FAILED_ATTEMPT / "attempt-header.json").read_text(encoding="utf-8"))
    assert header == {
        "schema_version": "legacy-pilot-attempt.k4d.v1",
        "authorization_id": "K4D-PILOT-30fb6c198a0e",
        "authorization_marker": "S1-07K4D-PILOT-REAL-MIGRATION-V1",
        "plan_sha256": _FROZEN_PLAN_PLAN_SHA256,
        "idempotency_key": "k4d-pilot-20260816-v1",
        "status": "STARTED",
    }
    failure = json.loads((_FAILED_ATTEMPT / "failure.json").read_text(encoding="utf-8"))
    assert failure == {
        "schema_version": "legacy-pilot-attempt.k4d.v1",
        "authorization_id": "K4D-PILOT-30fb6c198a0e",
        "code": "PILOT_SERVICE_FETCH_FAILED:000858.SZ",
        "status": "FAILED",
    }
    assert sorted(path.name for path in _FAILED_ATTEMPT.iterdir()) == [
        "attempt-header.json",
        "failure.json",
    ]


def test_second_real_pilot_attempt_failure_evidence_is_frozen_as_failed() -> None:
    """The 2026-08-17 second authorized pilot attempt stays a frozen failure.

    All 18 A/B/C endpoint calls succeeded (journaled per call); the attempt
    stopped fail-fast at the canonical-count guard because the real legacy
    files isolate 8,609 rows under the K4D semantics, contradicting the
    frozen zero-isolation expectation of 26,194 canonical rows. Staging
    writes stayed zero and the evidence must never become a success.
    """

    header = json.loads((_FAILED_ATTEMPT_2 / "attempt-header.json").read_text(encoding="utf-8"))
    assert header == {
        "schema_version": "legacy-pilot-attempt.k4d.v1",
        "authorization_id": "K4D-PILOT-d0280d367480",
        "authorization_marker": "S1-07K4D-PILOT-REAL-MIGRATION-V1",
        "plan_sha256": _FROZEN_PLAN_PLAN_SHA256,
        "idempotency_key": "k4d-pilot-20260817-v1",
        "status": "STARTED",
    }
    failure = json.loads((_FAILED_ATTEMPT_2 / "failure.json").read_text(encoding="utf-8"))
    assert failure == {
        "schema_version": "legacy-pilot-attempt.k4d.v1",
        "authorization_id": "K4D-PILOT-d0280d367480",
        "code": "PILOT_CANONICAL_COUNT_UNEXPECTED",
        "status": "FAILED",
    }
    assert sorted(path.name for path in _FAILED_ATTEMPT_2.iterdir()) == [
        "attempt-header.json",
        "failure.json",
        "invocations.jsonl",
    ]
    records = _journal_lines(_FAILED_ATTEMPT_2 / "invocations.jsonl")
    assert len(records) == 24
    assert [record["sequence"] for record in records] == list(range(1, 25))
    endpoints = [record for record in records if record["scope"] == "ENDPOINT"]
    symbols = [record for record in records if record["scope"] == "SYMBOL"]
    assert len(endpoints) == 18
    assert all(record["status"] == "SUCCESS" for record in records)
    expected_window_rows = {
        "000001.SZ": 263,
        "000858.SZ": 263,
        "300750.SZ": 263,
        "600000.SH": 263,
        "688237.SH": 258,
        "688981.SH": 257,
    }
    for record in symbols:
        expected = expected_window_rows[record["symbol"]]
        assert set(record["row_counts"]) == {"BAOSTOCK", "TENCENT", "SINA"}
        assert all(value == expected for value in record["row_counts"].values())
    raw = _ATTEMPT_2_DIAGNOSTIC.read_bytes()
    assert sha256_bytes(raw) == _ATTEMPT_2_DIAGNOSTIC_SHA256
    diagnostic = json.loads(raw)
    assert diagnostic["trigger"] == "PILOT_CANONICAL_COUNT_UNEXPECTED"
    assert diagnostic["combined_transform"]["canonical_row_count"] == 17_585
    assert diagnostic["combined_transform"]["isolated_row_count"] == 8_609
    assert diagnostic["combined_transform"]["conservation_holds"] is True
