from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from quantking.cli.legacy_import import run_cli
from quantking.data.legacy_import.contracts import (
    LegacyImportSemantics,
    canonical_json_bytes,
)
from quantking.data.models import DatasetKind


def _rows() -> list[dict[str, object]]:
    return [
        {
            "code": "600000.SH",
            "trade_date": date(2026, 6, 10),
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.0,
            "pre_close": 9.9,
            "volume": 100,
            "amount": 1_000.0,
            "suspended": False,
        },
        {
            "code": "600000.SH",
            "trade_date": date(2026, 6, 11),
            "open": 8.0,
            "high": 8.2,
            "low": 7.8,
            "close": 8.0,
            "pre_close": 10.0,
            "volume": 200,
            "amount": 1_600.0,
            "suspended": False,
        },
    ]


def _write_source(source_root: Path) -> None:
    target = source_root / "data" / "kline" / "1d" / "600000.SH.parquet"
    target.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(_rows()), target)


def _semantics_payload() -> dict[str, object]:
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
    return LegacyImportSemantics(
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
    ).to_payload()


def _invoke(argv: list[str]) -> tuple[int, dict[str, object]]:
    stream = io.StringIO()
    code = run_cli(argv, stdout=stream)
    return code, json.loads(stream.getvalue())


def _inventory_path(tmp_path: Path) -> Path:
    inventory = tmp_path / "inventory.json"
    inventory.write_bytes(canonical_json_bytes(_inventory_payload(tmp_path)))
    return inventory


def _inventory_payload(tmp_path: Path) -> dict[str, object]:
    from quantking.data.adapters.legacy_import import LegacyInventoryPlanner
    from quantking.data.legacy_import.contracts import (
        K3C_SYNTHETIC_AUTHORIZATION_MARKER,
        LegacyImportAuthorization,
    )

    source_root = tmp_path / "source"
    staging_root = tmp_path / "managed"
    authorization = LegacyImportAuthorization(
        source_root=source_root,
        staging_root=staging_root,
        marker=K3C_SYNTHETIC_AUTHORIZATION_MARKER,
    )
    planner = LegacyInventoryPlanner(authorization)
    return planner.inventory(planner.discover()).to_payload()


def _semantics_path(tmp_path: Path) -> Path:
    semantics = tmp_path / "semantics.json"
    semantics.write_bytes(canonical_json_bytes(_semantics_payload()))
    return semantics


def _run_once(argv: list[str]) -> tuple[int, dict[str, object]]:
    return _invoke(argv)


def test_inventory_command_writes_one_canonical_inventory_line(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    _write_source(source_root)
    code, payload = _invoke(
        [
            "inventory",
            "--source-root",
            str(source_root),
            "--staging-root",
            str(tmp_path / "managed"),
            "--marker",
            "synthetic",
        ]
    )
    assert code == 0
    assert payload["schema_version"] == "legacy-inventory.k3c.v1"
    assert payload["file_count"] == 1
    assert payload["total_bytes"] > 0
    assert payload["readable_rows"] == 2
    assert len(payload["source_fingerprint"]) == 64
    assert payload["entries"][0]["relative_path"] == "data/kline/1d/600000.SH.parquet"


def test_import_dry_run_does_not_read_source_or_write_staging(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    _write_source(source_root)
    staging = tmp_path / "managed"
    code, payload = _invoke(
        [
            "import",
            "--source-root",
            str(source_root),
            "--staging-root",
            str(staging),
            "--marker",
            "synthetic",
            "--inventory-file",
            str(_inventory_path(tmp_path)),
            "--semantics-file",
            str(_semantics_path(tmp_path)),
            "--idempotency-key",
            "pilot-1",
            "--mode",
            "PILOT",
            "--dry-run",
        ]
    )
    assert code == 0
    assert payload["schema_version"] == "legacy-import-dry-run.k3c.v1"
    assert payload["reads_source"] is False
    assert payload["writes_staging"] is False
    assert payload["declared_file_count"] == 1
    assert payload["service_network_invocation_upper_bound"] == 0
    assert not staging.exists()


def test_import_executes_then_reuses_same_batch(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    _write_source(source_root)
    staging = tmp_path / "managed"
    common = [
        "import",
        "--source-root",
        str(source_root),
        "--staging-root",
        str(staging),
        "--marker",
        "synthetic",
        "--inventory-file",
        str(_inventory_path(tmp_path)),
        "--semantics-file",
        str(_semantics_path(tmp_path)),
        "--idempotency-key",
        "pilot-1",
    ]
    first_code, first = _invoke(common)
    assert first_code == 0
    assert first["created"] is True
    assert first["reused"] is False
    assert first["warehouse_published"] is False
    assert first["canonical_row_count"] == 2
    assert len(first["manifest_sha256"]) == 64
    assert (staging / "legacy-imports").is_dir()
    second_code, second = _invoke(common)
    assert second_code == 0
    assert second["created"] is False
    assert second["reused"] is True
    assert second["batch_id"] == first["batch_id"]
    assert second["manifest_sha256"] == first["manifest_sha256"]


def test_import_quarantines_inventory_pointing_at_missing_source(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    staging = tmp_path / "managed"
    code, payload = _invoke(
        [
            "import",
            "--source-root",
            str(source_root),
            "--staging-root",
            str(staging),
            "--marker",
            "synthetic",
            "--inventory-file",
            str(_inventory_path(tmp_path)),
            "--semantics-file",
            str(_semantics_path(tmp_path)),
            "--idempotency-key",
            "missing-source",
        ]
    )
    assert code == 1
    assert payload["error"]["code"] in {
        "LEGACY_SOURCE_ERROR",
        "LEGACY_INPUT_INVALID",
    }
    assert (staging / "legacy-import-quarantine").is_dir()


def test_invalid_arguments_return_stable_error() -> None:
    code, payload = _invoke(["inventory", "--marker", "synthetic"])
    assert code == 2
    assert payload["error"]["code"] == "INVALID_ARGUMENT"
