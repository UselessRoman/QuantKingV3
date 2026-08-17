"""S1-07K4D inventory dry-run plan and closed-scan execution contract tests."""

from __future__ import annotations

import io
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quantking.cli.legacy_import import run_cli
from quantking.data.adapters.legacy_import import (
    LegacyCandidateSetChanged,
    LegacyInventoryPlanner,
)
from quantking.data.legacy_import import (
    K4D_FULL_REAL_MARKER,
    K4D_INVENTORY_REAL_MARKER,
    K4D_INVENTORY_SYNTHETIC_MARKER,
    K4D_PILOT_REAL_MARKER,
    LEGACY_INVENTORY_DRY_RUN_SCHEMA_VERSION,
    LEGACY_STAGE0_BASELINE_FILE_COUNT,
    LEGACY_STAGE0_BASELINE_TOTAL_BYTES,
    LegacyImportAuthorization,
    LegacyImportMode,
    LegacyInventoryDryRun,
)
from quantking.data.legacy_import.contracts import (
    LegacyImportSemantics,
    canonical_json_bytes,
    inventory_from_payload,
    sha256_bytes,
)
from quantking.data.models import DatasetKind

_ROOT = Path(__file__).resolve().parents[2]
_EVIDENCE_RELATIVE = "docs/stage1/evidence/k4d_inventory_dry_run_2026-08-16.json"
_EVIDENCE_SHA256 = "46272de4a88d57ec879bc4871f49d8d39294add0fa6ca2386fdabbeaafa229c4"
_PLAN_SHA256 = "f4fee8285214625e308ab028e76e2373bcc33d3a3b1ffff4b95cede4a8ce41df"
_REAL_SOURCE_ROOT = Path(r"E:\QuantKing\data\kline\1d")
_REAL_STAGING_ROOT = Path(r"E:\QuantKingV3Data")
_INVENTORY_EVIDENCE_RELATIVE = "docs/stage1/evidence/k4d_inventory_2026-08-16.json"
_INVENTORY_EVIDENCE_SHA256 = "140978ea3e3f4d9139c15d641c92564af8f58928bf77017117393ce988a04ce2"
_INVENTORY_SOURCE_FINGERPRINT = "af42dd43b2aedd9707e3b1761f5fcfc80370c213985e41530312b33a4b4b710f"


def _authorization(
    tmp_path: Path, marker: str = K4D_INVENTORY_SYNTHETIC_MARKER
) -> LegacyImportAuthorization:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    return LegacyImportAuthorization(
        source_root=source,
        staging_root=tmp_path / "staging",
        mode=LegacyImportMode.INVENTORY,
        marker=marker,
    )


def _row(code: str, trade_date: date) -> dict[str, object]:
    return {
        "code": code,
        "trade_date": trade_date,
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.2,
        "pre_close": 10.0,
        "volume": 100,
        "amount": 1_000.0,
    }


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def _invoke_raw(argv: list[str]) -> tuple[int, str]:
    stream = io.StringIO()
    code = run_cli(argv, stdout=stream)
    return code, stream.getvalue()


def _invoke(argv: list[str]) -> tuple[int, dict[str, Any]]:
    code, raw = _invoke_raw(argv)
    return code, json.loads(raw)


def _dry_run_arguments(source_root: str, staging_root: str) -> list[str]:
    return [
        "inventory",
        "--dry-run",
        "--source-root",
        source_root,
        "--staging-root",
        staging_root,
        "--marker",
        "real",
    ]


def test_inventory_dry_run_outputs_one_canonical_json_plan_line() -> None:
    code, raw = _invoke_raw(_dry_run_arguments(str(_REAL_SOURCE_ROOT), str(_REAL_STAGING_ROOT)))
    assert code == 0
    assert raw.count("\n") == 1
    payload = json.loads(raw)
    assert raw == (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    assert payload["schema_version"] == LEGACY_INVENTORY_DRY_RUN_SCHEMA_VERSION
    assert payload["schema_version"] == "legacy-inventory-dry-run.k4d.v1"
    assert payload["mode"] == "INVENTORY"
    assert payload["authorization_marker"] == "S1-07K4D-INVENTORY-REAL-READONLY-V1"
    assert payload["source_root"] == r"E:\QuantKing\data\kline\1d"
    assert payload["managed_staging_root"] == r"E:\QuantKingV3Data"
    assert payload["candidate_rule"] == "recursive *.parquet"
    assert payload["follow_symlinks_or_junctions"] is False
    assert payload["stage0_baseline_file_count"] == LEGACY_STAGE0_BASELINE_FILE_COUNT == 5_208
    assert payload["stage0_baseline_bytes"] == LEGACY_STAGE0_BASELINE_TOTAL_BYTES == 527_708_270
    assert payload["stage0_baseline_role"] == "difference-comparison-only"
    assert payload["service_network_invocation_upper_bound"] == 0
    assert payload["application_retries"] == 0
    assert payload["reads_source"] is False
    assert payload["writes_source"] is False
    assert payload["writes_staging"] is False
    assert payload["publishes_warehouse"] is False
    assert payload["requires_new_authorization_for_execution"] is True
    unsigned = {key: value for key, value in payload.items() if key != "plan_sha256"}
    assert payload["plan_sha256"] == sha256_bytes(canonical_json_bytes(unsigned))


def test_inventory_dry_run_with_real_marker_never_touches_the_filesystem(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "absent-source"
    staging_root = tmp_path / "absent-staging"

    def forbid(name: str) -> Any:
        def raiser(self: Path, *args: object, **kwargs: object) -> Any:
            raise AssertionError(f"inventory dry-run must not call Path.{name}")

        return raiser

    with pytest.MonkeyPatch.context() as patcher:
        for name in (
            "exists",
            "is_dir",
            "is_file",
            "stat",
            "resolve",
            "open",
            "mkdir",
            "rglob",
            "glob",
        ):
            patcher.setattr(Path, name, forbid(name))
        code, payload = _invoke(_dry_run_arguments(str(source_root), str(staging_root)))
    assert code == 0
    assert payload["authorization_marker"] == K4D_INVENTORY_REAL_MARKER
    assert payload["reads_source"] is False
    assert payload["writes_staging"] is False
    assert not source_root.exists()
    assert not staging_root.exists()


def test_inventory_dry_run_never_discovers_walks_or_reads_parquet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> Any:
        raise AssertionError("inventory dry-run must not discover, walk, or read parquet")

    monkeypatch.setattr(LegacyInventoryPlanner, "discover", boom)
    monkeypatch.setattr(LegacyInventoryPlanner, "inventory", boom)
    monkeypatch.setattr(LegacyInventoryPlanner, "closed_scan", boom)
    monkeypatch.setattr(os, "walk", boom)
    monkeypatch.setattr(pq, "read_table", boom)
    code, payload = _invoke(_dry_run_arguments(str(tmp_path / "source"), str(tmp_path / "staging")))
    assert code == 0
    assert payload["reads_source"] is False


def test_inventory_dry_run_does_not_create_or_write_staging_root(tmp_path: Path) -> None:
    staging_root = tmp_path / "staging"
    code, _ = _invoke(_dry_run_arguments(str(tmp_path / "source" / "kline"), str(staging_root)))
    assert code == 0
    assert not staging_root.exists()
    assert list(tmp_path.iterdir()) == []


def test_inventory_dry_run_rejects_cross_phase_markers(tmp_path: Path) -> None:
    for marker in (K4D_PILOT_REAL_MARKER, K4D_FULL_REAL_MARKER):
        with pytest.raises(ValueError, match="INVENTORY mode"):
            LegacyInventoryDryRun(
                source_root=tmp_path / "source",
                staging_root=tmp_path / "staging",
                marker=marker,
            )
    for bad in ((tmp_path / "source", tmp_path / "source"),):
        with pytest.raises(ValueError, match="distinct"):
            LegacyInventoryDryRun(
                source_root=bad[0],
                staging_root=bad[1],
                marker=K4D_INVENTORY_REAL_MARKER,
            )
    with pytest.raises(ValueError, match="absolute Path"):
        LegacyInventoryDryRun(
            source_root=Path("relative/source"),
            staging_root=tmp_path / "staging",
            marker=K4D_INVENTORY_REAL_MARKER,
        )


def test_closed_scan_matches_discover_plus_inventory_and_keeps_k3c_schema(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    _write(
        authorization.source_root / "daily/000001.SZ.parquet",
        [_row("000001.SZ", date(2026, 6, 10))],
    )
    _write(
        authorization.source_root / "daily/600000.SH.parquet", [_row("600000.SH", date(2026, 8, 9))]
    )
    planner = LegacyInventoryPlanner(authorization)

    closed = planner.closed_scan()
    reference = planner.inventory(planner.discover())

    assert closed == reference
    payload = closed.to_payload()
    assert payload["schema_version"] == "legacy-inventory.k3c.v1"
    assert payload["file_count"] == 2
    assert inventory_from_payload(payload) == closed


def test_cli_inventory_runs_the_closed_scan_for_synthetic_fixtures(tmp_path: Path) -> None:
    authorization = _authorization(tmp_path)
    _write(
        authorization.source_root / "daily/sample.parquet", [_row("600000.SH", date(2026, 8, 8))]
    )
    code, payload = _invoke(
        [
            "inventory",
            "--source-root",
            str(authorization.source_root),
            "--staging-root",
            str(tmp_path / "staging"),
            "--marker",
            "synthetic",
        ]
    )
    assert code == 0
    assert payload["schema_version"] == "legacy-inventory.k3c.v1"
    assert payload["file_count"] == 1
    assert inventory_from_payload(payload).file_count == 1


def test_closed_scan_fails_when_a_candidate_is_added_during_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization = _authorization(tmp_path)
    _write(
        authorization.source_root / "daily/sample.parquet", [_row("600000.SH", date(2026, 8, 8))]
    )
    original = LegacyInventoryPlanner.inventory

    def inventory_then_add(self: LegacyInventoryPlanner, paths: tuple[str, ...]) -> Any:
        result = original(self, paths)
        _write(
            self._authorization.source_root / "daily/new.parquet",
            [_row("000001.SZ", date(2026, 8, 8))],
        )
        return result

    monkeypatch.setattr(LegacyInventoryPlanner, "inventory", inventory_then_add)
    with pytest.raises(LegacyCandidateSetChanged, match="candidate set changed"):
        LegacyInventoryPlanner(authorization).closed_scan()


def test_closed_scan_fails_when_a_candidate_is_removed_during_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization = _authorization(tmp_path)
    _write(authorization.source_root / "daily/keep.parquet", [_row("600000.SH", date(2026, 8, 8))])
    _write(authorization.source_root / "daily/drop.parquet", [_row("000001.SZ", date(2026, 8, 8))])
    original = LegacyInventoryPlanner.inventory

    def inventory_then_remove(self: LegacyInventoryPlanner, paths: tuple[str, ...]) -> Any:
        result = original(self, paths)
        (self._authorization.source_root / "daily/drop.parquet").unlink()
        return result

    monkeypatch.setattr(LegacyInventoryPlanner, "inventory", inventory_then_remove)
    with pytest.raises(LegacyCandidateSetChanged, match="candidate set changed"):
        LegacyInventoryPlanner(authorization).closed_scan()


def test_closed_scan_fails_when_candidate_metadata_changes_during_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization = _authorization(tmp_path)
    _write(
        authorization.source_root / "daily/sample.parquet", [_row("600000.SH", date(2026, 8, 8))]
    )
    original = LegacyInventoryPlanner.inventory

    def inventory_then_mutate(self: LegacyInventoryPlanner, paths: tuple[str, ...]) -> Any:
        result = original(self, paths)
        target = self._authorization.source_root / "daily/sample.parquet"
        with target.open("ab") as stream:
            stream.write(b"trailing-mutation")
        return result

    monkeypatch.setattr(LegacyInventoryPlanner, "inventory", inventory_then_mutate)
    with pytest.raises(LegacyCandidateSetChanged, match="candidate metadata changed"):
        LegacyInventoryPlanner(authorization).closed_scan()


def test_closed_scan_failure_emits_no_successful_inventory_via_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization = _authorization(tmp_path)
    _write(
        authorization.source_root / "daily/sample.parquet", [_row("600000.SH", date(2026, 8, 8))]
    )
    original = LegacyInventoryPlanner.inventory

    def inventory_then_add(self: LegacyInventoryPlanner, paths: tuple[str, ...]) -> Any:
        result = original(self, paths)
        _write(
            self._authorization.source_root / "daily/late.parquet",
            [_row("000001.SZ", date(2026, 8, 8))],
        )
        return result

    monkeypatch.setattr(LegacyInventoryPlanner, "inventory", inventory_then_add)
    code, payload = _invoke(
        [
            "inventory",
            "--source-root",
            str(authorization.source_root),
            "--staging-root",
            str(tmp_path / "staging"),
            "--marker",
            "synthetic",
        ]
    )
    assert code == 1
    assert payload["error"]["code"] == "LEGACY_SOURCE_ERROR"
    assert "source_fingerprint" not in payload


def test_frozen_k4d_inventory_dry_run_evidence_is_canonical_and_stable() -> None:
    raw = (_ROOT / _EVIDENCE_RELATIVE).read_bytes()
    payload = json.loads(raw)
    assert canonical_json_bytes(payload) + b"\n" == raw
    expected = LegacyInventoryDryRun(
        source_root=_REAL_SOURCE_ROOT,
        staging_root=_REAL_STAGING_ROOT,
        marker=K4D_INVENTORY_REAL_MARKER,
    ).to_payload()
    assert payload == expected
    assert payload["plan_sha256"] == _PLAN_SHA256
    assert sha256_bytes(raw) == _EVIDENCE_SHA256
    assert payload["reads_source"] is False
    assert payload["service_network_invocation_upper_bound"] == 0
    assert payload["writes_staging"] is False


def test_frozen_k4d_real_inventory_evidence_is_byte_stable_and_readable() -> None:
    """The authorized 2026-08-16 read-only inventory stays frozen and re-readable.

    The evidence records the Stage 0 baseline exactly (5,208 files /
    527,708,270 bytes, difference 0/0), so any byte change to the old root or
    to this file breaks a later re-verification.
    """

    raw = (_ROOT / _INVENTORY_EVIDENCE_RELATIVE).read_bytes()
    payload = json.loads(raw)
    assert sha256_bytes(raw) == _INVENTORY_EVIDENCE_SHA256
    assert payload["schema_version"] == "legacy-inventory.k3c.v1"
    inventory = inventory_from_payload(payload)
    assert inventory.source_fingerprint == _INVENTORY_SOURCE_FINGERPRINT
    assert inventory.file_count == LEGACY_STAGE0_BASELINE_FILE_COUNT
    assert inventory.total_bytes == LEGACY_STAGE0_BASELINE_TOTAL_BYTES
    assert inventory.readable_rows == 16_786_177
    assert all(entry.read_error is None for entry in inventory.entries)
    assert all(entry.sha256 is not None for entry in inventory.entries)
    assert sum(entry.duplicate_key_count or 0 for entry in inventory.entries) == 0


def _confirmed_semantics_payload() -> dict[str, object]:
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
    semantics = LegacyImportSemantics(
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
    assert semantics.confirmed
    return semantics.to_payload()


def test_frozen_real_inventory_loads_through_the_import_cli_loader(tmp_path: Path) -> None:
    """The frozen inventory evidence must be consumable by the import command as-is.

    The evidence file is one canonical JSON line (trailing LF included) exactly
    as written by ``inventory``; the real ``_load_json_file`` behind
    ``import --inventory-file`` must accept it without any offline rewrite.
    """

    semantics = tmp_path / "semantics.json"
    semantics.write_bytes(canonical_json_bytes(_confirmed_semantics_payload()))
    code, payload = _invoke(
        [
            "import",
            "--dry-run",
            "--source-root",
            str(tmp_path / "source"),
            "--staging-root",
            str(tmp_path / "staging"),
            "--marker",
            "synthetic",
            "--inventory-file",
            str(_ROOT / _INVENTORY_EVIDENCE_RELATIVE),
            "--semantics-file",
            str(semantics),
            "--idempotency-key",
            "k4d-frozen-inventory",
        ]
    )
    assert code == 0
    assert payload["schema_version"] == "legacy-import-dry-run.k3c.v1"
    assert payload["declared_file_count"] == 5_208
    assert payload["declared_bytes"] == 527_708_270
    assert payload["declared_rows"] == 16_786_177
    assert payload["reads_source"] is False
    assert payload["writes_staging"] is False
