from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quantking.common.errors import IdempotencyConflictError, InvalidPathError
from quantking.data.adapters.legacy_import import (
    LegacyImportError,
    LegacyInventoryPlanner,
)
from quantking.data.adapters.legacy_managed_import import (
    LegacyImportQuarantined,
    LegacyManagedImporter,
    verify_managed_legacy_import,
)
from quantking.data.legacy_import import (
    K4D_PILOT_SYNTHETIC_MARKER,
    LegacyImportAuthorization,
    LegacyImportMode,
    LegacyImportSemantics,
)
from quantking.data.models import DatasetKind


def _authorization(tmp_path: Path) -> LegacyImportAuthorization:
    source = tmp_path / "source"
    source.mkdir()
    return LegacyImportAuthorization(
        source_root=source,
        staging_root=tmp_path / "managed",
        mode=LegacyImportMode.PILOT,
        marker=K4D_PILOT_SYNTHETIC_MARKER,
        pilot_symbols=("600000.SH",),
    )


def _semantics() -> LegacyImportSemantics:
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
    )


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
        {  # ex-dividend boundary: price may jump, pre_close remains prior raw close
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
        {  # suspension semantics
            "code": "600000.SH",
            "trade_date": date(2026, 6, 12),
            "open": 8.0,
            "high": 8.0,
            "low": 8.0,
            "close": 8.0,
            "pre_close": 8.0,
            "volume": 0,
            "amount": 0.0,
            "suspended": True,
        },
        {  # low-liquidity row
            "code": "600000.SH",
            "trade_date": date(2026, 6, 13),
            "open": 8.0,
            "high": 8.1,
            "low": 8.0,
            "close": 8.1,
            "pre_close": 8.0,
            "volume": 1,
            "amount": 8.1,
            "suspended": False,
        },
    ]


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def _inventory(
    authorization: LegacyImportAuthorization,
):
    planner = LegacyInventoryPlanner(authorization)
    paths = planner.discover()
    inventory = planner.inventory(paths)
    assert tuple(entry.relative_path for entry in inventory.entries) == paths
    return inventory


def _signed(payload: dict[str, object]) -> tuple[bytes, str]:
    unsigned = {key: value for key, value in payload.items() if key != "manifest_sha256"}
    raw = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    digest = hashlib.sha256(raw).hexdigest()
    return (
        json.dumps(
            {**unsigned, "manifest_sha256": digest},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
        digest,
    )


def _resign(directory: Path, manifest: dict[str, object]) -> None:
    raw, digest = _signed(manifest)
    (directory / "manifest.json").write_bytes(raw)
    (directory / "COMPLETE").write_bytes(
        json.dumps(
            {"batch_id": manifest["batch_id"], "manifest_sha256": digest},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )


def test_managed_copy_preserves_raw_builds_unverified_candidate_and_never_publishes(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    source = authorization.source_root / "daily/sample.parquet"
    _write(source, _rows())
    source_before = source.read_bytes()
    inventory = _inventory(authorization)

    outcome = LegacyManagedImporter(authorization).import_inventory(
        inventory=inventory,
        semantics=_semantics(),
        idempotency_key="pilot-001",
    )

    assert outcome.created is True and outcome.reused is False
    assert outcome.warehouse_published is False
    assert outcome.canonical_row_count == inventory.readable_rows == 4
    assert outcome.evidence.x_only_unverified_rows == 4
    assert outcome.evidence.x_a_consensus_rows == 0
    assert source.read_bytes() == source_before
    raw_copy = outcome.directory / "raw/daily/sample.parquet"
    assert raw_copy.read_bytes() == source_before
    assert not os.path.samefile(source, raw_copy)
    manifest, digest = verify_managed_legacy_import(
        outcome.directory, staging_root=authorization.staging_root
    )
    assert digest == outcome.manifest_sha256
    assert manifest["status"] == "STAGED_X_ONLY_UNVERIFIED"
    assert manifest["warehouse_published"] is False
    assert manifest["first_row_pre_close_unverified_count"] == 1
    assert not (authorization.staging_root / "warehouse").exists()


def test_same_manifest_reuses_bytes_and_source_change_creates_new_batch(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    source = authorization.source_root / "daily/sample.parquet"
    _write(source, _rows())
    inventory = _inventory(authorization)
    importer = LegacyManagedImporter(authorization)
    first = importer.import_inventory(
        inventory=inventory, semantics=_semantics(), idempotency_key="pilot-001"
    )
    snapshot = {
        path.relative_to(first.directory).as_posix(): (
            path.stat().st_mtime_ns,
            path.read_bytes(),
        )
        for path in first.directory.rglob("*")
        if path.is_file()
    }
    same_key = importer.import_inventory(
        inventory=inventory, semantics=_semantics(), idempotency_key="pilot-001"
    )
    new_key_same_content = importer.import_inventory(
        inventory=inventory, semantics=_semantics(), idempotency_key="pilot-002"
    )
    assert same_key.reused and new_key_same_content.reused
    assert same_key.batch_id == new_key_same_content.batch_id == first.batch_id
    assert snapshot == {
        path.relative_to(first.directory).as_posix(): (
            path.stat().st_mtime_ns,
            path.read_bytes(),
        )
        for path in first.directory.rglob("*")
        if path.is_file()
    }
    changed_rows = _rows()
    changed_rows[-1]["volume"] = 2
    _write(source, changed_rows)
    changed = _inventory(authorization)
    with pytest.raises(IdempotencyConflictError, match="different content"):
        importer.import_inventory(
            inventory=changed,
            semantics=_semantics(),
            idempotency_key="pilot-001",
        )
    second = importer.import_inventory(
        inventory=changed, semantics=_semantics(), idempotency_key="pilot-003"
    )
    assert second.batch_id != first.batch_id
    assert second.directory != first.directory


@pytest.mark.parametrize(
    "mutation",
    (
        {"unadjusted": False},
        {"volume_unit": "lot"},
        {"amount_unit": "unknown"},
        {"timezone": "unknown"},
        {"suspension_semantics": "unknown"},
        {"null_semantics": "unknown"},
    ),
)
def test_unknown_semantics_quarantines_without_warehouse(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    authorization = _authorization(tmp_path)
    _write(authorization.source_root / "daily/sample.parquet", _rows())
    semantics = replace(_semantics(), **mutation)

    with pytest.raises(LegacyImportQuarantined, match="not fully confirmed"):
        LegacyManagedImporter(authorization).import_inventory(
            inventory=_inventory(authorization),
            semantics=semantics,
            idempotency_key="pilot-invalid",
        )

    quarantines = list((authorization.staging_root / "legacy-import-quarantine").glob("*"))
    assert len(quarantines) == 1
    assert (quarantines[0] / "QUARANTINE.json").is_file()
    assert not list((authorization.staging_root / "legacy-imports").glob("data_batch--*"))
    assert not (authorization.staging_root / "warehouse").exists()


@pytest.mark.parametrize("case", ("duplicate", "null", "schema", "pre_close", "suspension"))
def test_invalid_data_is_quarantined_without_published_candidate(tmp_path: Path, case: str) -> None:
    authorization = _authorization(tmp_path)
    rows = _rows()
    semantics = _semantics()
    if case == "duplicate":
        rows.append(dict(rows[-1]))
    elif case == "null":
        rows[-1]["amount"] = None
    elif case == "schema":
        rows[-1].pop("volume")
    elif case == "pre_close":
        rows[-1]["pre_close"] = 999.0
    elif case == "suspension":
        rows[2]["volume"] = 1
    _write(authorization.source_root / "daily/sample.parquet", rows)

    with pytest.raises(LegacyImportQuarantined):
        LegacyManagedImporter(authorization).import_inventory(
            inventory=_inventory(authorization),
            semantics=semantics,
            idempotency_key="pilot-invalid",
        )

    assert not list((authorization.staging_root / "legacy-imports").glob("data_batch--*"))
    assert not (authorization.staging_root / "warehouse").exists()


def test_interrupted_copy_is_quarantined_and_does_not_mutate_source(tmp_path: Path) -> None:
    authorization = _authorization(tmp_path)
    source = authorization.source_root / "daily/sample.parquet"
    _write(source, _rows())
    before = source.read_bytes()

    def corrupt(copy: Path) -> None:
        copy.write_bytes(copy.read_bytes() + b"interrupted")

    with pytest.raises(LegacyImportQuarantined, match="interrupted or changed"):
        LegacyManagedImporter(authorization, after_copy_hook=corrupt).import_inventory(
            inventory=_inventory(authorization),
            semantics=_semantics(),
            idempotency_key="pilot-interrupted",
        )

    assert source.read_bytes() == before
    assert not list((authorization.staging_root / "legacy-imports").glob("data_batch--*"))
    assert len(list((authorization.staging_root / "legacy-import-quarantine").glob("*"))) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("provider", "AKSHARE"),
        ("status", "PUBLISHED"),
        ("warehouse_published", True),
        ("raw_row_count", 999),
        ("first_row_pre_close_unverified_count", 0),
    ),
)
def test_verifier_rejects_fully_resigned_manifest_tamper(
    tmp_path: Path, field: str, value: object
) -> None:
    authorization = _authorization(tmp_path)
    _write(authorization.source_root / "daily/sample.parquet", _rows())
    outcome = LegacyManagedImporter(authorization).import_inventory(
        inventory=_inventory(authorization),
        semantics=_semantics(),
        idempotency_key="pilot-001",
    )
    manifest = json.loads((outcome.directory / "manifest.json").read_bytes())
    manifest[field] = value
    _resign(outcome.directory, manifest)

    with pytest.raises(LegacyImportError):
        verify_managed_legacy_import(outcome.directory, staging_root=authorization.staging_root)


def test_verifier_rejects_fully_resigned_canonical_or_raw_tamper(
    tmp_path: Path,
) -> None:
    authorization = _authorization(tmp_path)
    _write(authorization.source_root / "daily/sample.parquet", _rows())
    outcome = LegacyManagedImporter(authorization).import_inventory(
        inventory=_inventory(authorization),
        semantics=_semantics(),
        idempotency_key="pilot-001",
    )
    canonical = outcome.directory / "candidate/canonical.parquet"
    table = pq.read_table(canonical)
    rows = table.to_pylist()
    rows[-1]["volume"] = 99
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), canonical)
    manifest = json.loads((outcome.directory / "manifest.json").read_bytes())
    file_entry = next(
        item for item in manifest["files"] if item["relative_path"] == "candidate/canonical.parquet"
    )
    payload = canonical.read_bytes()
    file_entry["size"] = len(payload)
    file_entry["sha256"] = hashlib.sha256(payload).hexdigest()
    _resign(outcome.directory, manifest)

    with pytest.raises(LegacyImportError, match="does not rebuild"):
        verify_managed_legacy_import(outcome.directory, staging_root=authorization.staging_root)


def test_verifier_rejects_fully_resigned_semantics_or_raw_tamper(tmp_path: Path) -> None:
    authorization = _authorization(tmp_path)
    _write(authorization.source_root / "daily/sample.parquet", _rows())
    outcome = LegacyManagedImporter(authorization).import_inventory(
        inventory=_inventory(authorization),
        semantics=_semantics(),
        idempotency_key="pilot-001",
    )
    manifest = json.loads((outcome.directory / "manifest.json").read_bytes())
    manifest["semantics"]["volume_unit"] = "lot"
    _resign(outcome.directory, manifest)
    with pytest.raises(LegacyImportError):
        verify_managed_legacy_import(outcome.directory, staging_root=authorization.staging_root)

    manifest = json.loads((outcome.directory / "manifest.json").read_bytes())
    manifest["semantics"]["volume_unit"] = "share"
    raw = outcome.directory / "raw/daily/sample.parquet"
    raw.write_bytes(raw.read_bytes() + b"tamper")
    entry = next(item for item in manifest["files"] if item["relative_path"].startswith("raw/"))
    payload = raw.read_bytes()
    entry["size"] = len(payload)
    entry["sha256"] = hashlib.sha256(payload).hexdigest()
    _resign(outcome.directory, manifest)
    with pytest.raises(LegacyImportError):
        verify_managed_legacy_import(outcome.directory, staging_root=authorization.staging_root)


def test_verifier_rejects_path_escape_and_reparse_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorization = _authorization(tmp_path)
    _write(authorization.source_root / "daily/sample.parquet", _rows())
    outcome = LegacyManagedImporter(authorization).import_inventory(
        inventory=_inventory(authorization),
        semantics=_semantics(),
        idempotency_key="pilot-001",
    )
    with pytest.raises((InvalidPathError, LegacyImportError)):
        verify_managed_legacy_import(
            outcome.directory,
            staging_root=tmp_path / "different-managed-root",
        )
    monkeypatch.setattr(
        "quantking.data.adapters.legacy_managed_import.is_reparse_point",
        lambda path: path.name == "raw",
    )
    with pytest.raises(LegacyImportError, match="symlink or junction"):
        verify_managed_legacy_import(outcome.directory, staging_root=authorization.staging_root)


def test_pilot_subset_defers_non_selected_files(tmp_path: Path) -> None:
    authorization = _authorization(tmp_path)
    _write(authorization.source_root / "daily/600000.parquet", _rows())
    _write(
        authorization.source_root / "daily/000001.parquet",
        [dict(row, code="000001.SZ") for row in _rows()],
    )
    inventory = _inventory(authorization)
    assert inventory.file_count == 2

    outcome = LegacyManagedImporter(authorization).import_inventory(
        inventory=inventory,
        semantics=_semantics(),
        idempotency_key="pilot-subset",
    )

    assert outcome.deferred_file_count == 1
    assert outcome.deferred_byte_count > 0
    assert outcome.canonical_row_count == 4
    assert outcome.raw_file_count == 1
