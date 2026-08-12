from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quantking.data.adapters.legacy_import import (
    LegacyImportError,
    LegacyInventoryPlanner,
    LegacyRuntimeFetchRejected,
    assert_unchanged_inventory,
    reject_xtquant_runtime_fetch,
)
from quantking.data.legacy_import import (
    K3C_SYNTHETIC_AUTHORIZATION_MARKER,
    LegacyImportAuthorization,
    LegacyImportDryRun,
    LegacyImportMode,
)


def _authorization(tmp_path: Path) -> LegacyImportAuthorization:
    source = tmp_path / "source"
    source.mkdir()
    return LegacyImportAuthorization(
        source_root=source,
        staging_root=tmp_path / "staging",
        marker=K3C_SYNTHETIC_AUTHORIZATION_MARKER,
    )


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def _row(
    code: str,
    trade_date: date,
    *,
    volume: int | None = 100,
    amount: float | None = 1_000.0,
) -> dict[str, object]:
    return {
        "code": code,
        "trade_date": trade_date,
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.2,
        "pre_close": 10.0,
        "volume": volume,
        "amount": amount,
    }


def test_inventory_records_sha_schema_rows_coverage_nulls_duplicates_and_fingerprint(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    rows = [
        _row("000001.SZ", date(2026, 6, 10), volume=0, amount=0.0),
        _row("000001.SZ", date(2026, 6, 11)),
        _row("600000.SH", date(2026, 8, 9), volume=None),
        _row("600000.SH", date(2026, 8, 9), volume=None),
    ]
    _write(authorization.source_root / "daily/sample.parquet", rows)

    first = LegacyInventoryPlanner(authorization).inventory(("daily/sample.parquet",))
    second = LegacyInventoryPlanner(authorization).inventory(("daily/sample.parquet",))

    assert first == second
    assert first.source_fingerprint == second.source_fingerprint
    assert first.file_count == 1
    assert first.readable_rows == 4
    entry = first.entries[0]
    assert entry.sha256 is not None and len(entry.sha256) == 64
    assert "trade_date: date32[day]" in (entry.parquet_schema or "")
    assert entry.code_min == "000001.SZ"
    assert entry.code_max == "600000.SH"
    assert entry.date_min == "2026-06-10"
    assert entry.date_max == "2026-08-09"
    assert entry.null_counts["volume"] == 2
    assert entry.duplicate_key_count == 1
    assert json.dumps(first.to_payload(), separators=(",", ":"), sort_keys=True)


def test_discovery_recursively_freezes_all_and_only_parquet_paths(tmp_path: Path) -> None:
    authorization = _authorization(tmp_path)
    _write(
        authorization.source_root / "z/sample.PARQUET",
        [_row("600000.SH", date(2026, 8, 8))],
    )
    _write(
        authorization.source_root / "a/nested/sample.parquet",
        [_row("000001.SZ", date(2026, 8, 8))],
    )
    (authorization.source_root / "ignored.txt").write_text("ignored", encoding="utf-8")

    assert LegacyInventoryPlanner(authorization).discover() == (
        "a/nested/sample.parquet",
        "z/sample.PARQUET",
    )


def test_inventory_preserves_stable_read_errors_without_absolute_paths(tmp_path: Path) -> None:
    authorization = _authorization(tmp_path)
    bad = authorization.source_root / "daily/bad.parquet"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not parquet")

    inventory = LegacyInventoryPlanner(authorization).inventory(
        ("daily/bad.parquet", "daily/missing.parquet")
    )

    assert [entry.read_error for entry in inventory.entries] == [
        "PARQUET_READ_ERROR",
        "FILE_NOT_FOUND",
    ]
    encoded = json.dumps(inventory.to_payload(), separators=(",", ":"), sort_keys=True)
    assert str(authorization.source_root) not in encoded


def test_inventory_rejects_path_escape_and_unsorted_or_duplicate_scope(tmp_path: Path) -> None:
    authorization = _authorization(tmp_path)
    planner = LegacyInventoryPlanner(authorization)

    with pytest.raises(ValueError, match="must not be absolute or traverse"):
        planner.inventory(("../escape.parquet",))
    with pytest.raises(LegacyImportError, match="sorted and unique"):
        planner.inventory(("b.parquet", "a.parquet"))
    with pytest.raises(LegacyImportError, match="sorted and unique"):
        planner.inventory(("a.parquet", "a.parquet"))


def test_inventory_rejects_reparse_path_instead_of_reporting_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization = _authorization(tmp_path)
    target = authorization.source_root / "linked/sample.parquet"
    _write(target, [_row("600000.SH", date(2026, 8, 8))])
    monkeypatch.setattr(
        "quantking.data.adapters.legacy_import.is_reparse_point",
        lambda path: path.name == "linked",
    )

    with pytest.raises(LegacyImportError, match="symlink or junction"):
        LegacyInventoryPlanner(authorization).inventory(("linked/sample.parquet",))


def test_source_mutation_during_read_is_quarantined_with_stable_code(tmp_path: Path) -> None:
    authorization = _authorization(tmp_path)
    target = authorization.source_root / "daily/sample.parquet"
    _write(target, [_row("600000.SH", date(2026, 8, 8))])

    def mutate(path: Path) -> None:
        path.write_bytes(path.read_bytes() + b"changed")

    inventory = LegacyInventoryPlanner(authorization, after_hash_hook=mutate).inventory(
        ("daily/sample.parquet",)
    )

    assert inventory.entries[0].read_error == "SOURCE_MUTATED_DURING_READ"
    assert inventory.entries[0].sha256 is None


def test_before_after_fingerprint_detects_source_change(tmp_path: Path) -> None:
    authorization = _authorization(tmp_path)
    target = authorization.source_root / "daily/sample.parquet"
    _write(target, [_row("600000.SH", date(2026, 8, 8))])
    planner = LegacyInventoryPlanner(authorization)
    before = planner.inventory(("daily/sample.parquet",))
    _write(target, [_row("600000.SH", date(2026, 8, 8), volume=101)])
    after = planner.inventory(("daily/sample.parquet",))

    with pytest.raises(LegacyImportError, match="fingerprint changed"):
        assert_unchanged_inventory(before, after)


def test_dry_run_is_pure_and_freezes_inventory_pilot_full_zero_network() -> None:
    for mode in LegacyImportMode:
        payload = LegacyImportDryRun(
            mode=mode,
            source_root_label="<explicit-source-root>",
            staging_root_label="<managed-staging-root>",
            relative_paths=("daily/a.parquet", "daily/b.parquet"),
            declared_file_count=2,
            declared_bytes=200,
            declared_rows=20,
        ).to_payload()
        assert payload["reads_source"] is False
        assert payload["writes_staging"] is False
        assert payload["service_network_invocation_upper_bound"] == 0
        assert payload["requires_new_authorization_for_execution"] is True


def test_xtquant_legacy_runtime_fetch_rejects_without_vendor_import() -> None:
    with pytest.raises(LegacyRuntimeFetchRejected, match="DATA_ONLY"):
        reject_xtquant_runtime_fetch()
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; from quantking.data.adapters.legacy_import import "
                "reject_xtquant_runtime_fetch; "
                "\ntry: reject_xtquant_runtime_fetch()"
                "\nexcept Exception: pass"
                "\nprint('xtquant' in sys.modules)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.stdout.strip() == "False"
    assert completed.stderr == ""
