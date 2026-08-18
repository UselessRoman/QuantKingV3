"""S1-08R3F row-by-row release proof: pure per-key checks + integration.

The integration path publishes a synthetic formal release through the shared
stack (fake gateways, temporary data root) and proves every published key
against the actual A/B raw evidence. The pure functions carry the full
tamper matrix (single source, common absence, tolerance edges, silent C,
published-vs-selected, DifferenceReport recompute) without touching any
data root.
"""

from __future__ import annotations

import gc
import sqlite3
from pathlib import Path

import pytest
from tests.integration.data.test_s1_08r3_formal_entry import _FormalEnv, _gateways

from quantking.data.adapters.release_verifier import (
    ROW_PROOF_FIELDS,
    ReleaseRowProofError,
    assert_dr_rows_recompute,
    assert_published_equals_selected,
    compare_row_pair,
    verify_release_row_proof,
)
from quantking.data.comparison.thresholds import k4_evidence_thresholds
from quantking.data.models import DatasetKind

_THRESHOLDS = k4_evidence_thresholds().for_dataset(DatasetKind.DAILY_BAR_UNADJUSTED)


@pytest.fixture()
def formal(tmp_path: Path) -> _FormalEnv:
    return _FormalEnv(tmp_path)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "open": 10.0,
        "high": 10.0,
        "low": 10.0,
        "close": 10.0,
        "pre_close": 10.0,
        "volume": 10_000,
        "amount": 100_000.0,
    }
    row.update(overrides)
    return row


def _pair(a: dict[str, object] | None, b: dict[str, object] | None) -> dict[str, object]:
    return compare_row_pair(
        code="000001.SZ",
        trade_date="2026-06-11",
        primary_row=a,
        fallback_row=b,
        thresholds=_THRESHOLDS,
    )


def test_frozen_thresholds_are_exact_and_relative_axis_disabled() -> None:
    for field_name in ("open", "high", "low", "close", "pre_close"):
        assert _THRESHOLDS[field_name].absolute == 0
        assert _THRESHOLDS[field_name].relative == 0
    for field_name in ("volume", "amount"):
        assert _THRESHOLDS[field_name].absolute == 50
        assert _THRESHOLDS[field_name].relative == 0


def test_single_source_row_is_rejected() -> None:
    with pytest.raises(ReleaseRowProofError, match="ROW_SINGLE_SOURCE"):
        _pair(_row(), None)
    with pytest.raises(ReleaseRowProofError, match="ROW_SINGLE_SOURCE"):
        _pair(None, _row())


def test_common_missing_row_is_rejected() -> None:
    with pytest.raises(ReleaseRowProofError, match="ROW_COMMON_MISSING"):
        _pair(None, None)


def test_any_price_difference_is_rejected() -> None:
    for field_name in ("open", "high", "low", "close", "pre_close"):
        with pytest.raises(ReleaseRowProofError, match="ROW_PRICE_MISMATCH"):
            _pair(_row(), _row(**{field_name: 10.01}))


def test_volume_amount_at_exactly_fifty_passes() -> None:
    pair = _pair(_row(volume=10_050, amount=100_050.0), _row())
    assert pair["fields"]["volume"]["absolute"] == "50"
    assert pair["fields"]["amount"]["absolute"] == "50"


def test_volume_amount_above_fifty_fails() -> None:
    with pytest.raises(ReleaseRowProofError, match="ROW_TOLERANCE_EXCEEDED"):
        _pair(_row(volume=10_051), _row())
    with pytest.raises(ReleaseRowProofError, match="ROW_TOLERANCE_EXCEEDED"):
        _pair(_row(amount=100_051.5), _row())


def test_relative_tolerance_is_not_enabled() -> None:
    # A tiny absolute difference with a huge relative ratio still passes:
    # the frozen K4 rule disables the relative axis entirely.
    pair = _pair(_row(volume=1), _row(volume=49))
    assert pair["fields"]["volume"]["absolute"] == "48"


def test_dr_rows_must_recompute_from_actual_values() -> None:
    a = _row()
    b = _row()
    base = {
        "classification": "BOTH",
        "primary_value": 10_060,
        "fallback_value": 10_000,
        "absolute_difference": "60",
        "relative_difference": "0.0059880024",
    }
    # A recorded row while no threshold is exceeded -> mismatch.
    with pytest.raises(ReleaseRowProofError, match="DR_MISMATCH"):
        assert_dr_rows_recompute(
            code="000001.SZ",
            trade_date="2026-06-11",
            primary_row=a,
            fallback_row=b,
            dr_rows=[{**base, "field": "volume"}],
            thresholds=_THRESHOLDS,
        )
    # Evidence that differs from the recomputed values -> mismatch.
    with pytest.raises(ReleaseRowProofError, match="DR_MISMATCH"):
        assert_dr_rows_recompute(
            code="000001.SZ",
            trade_date="2026-06-11",
            primary_row=_row(volume=10_060),
            fallback_row=b,
            dr_rows=[{**base, "absolute_difference": "61", "field": "volume"}],
            thresholds=_THRESHOLDS,
        )
    # Missing-row classifications are blockers.
    with pytest.raises(ReleaseRowProofError, match="DR_MISSING_ROW_RECORDED"):
        assert_dr_rows_recompute(
            code="000001.SZ",
            trade_date="2026-06-11",
            primary_row=a,
            fallback_row=b,
            dr_rows=[{"classification": "PRIMARY_ONLY", "field": ""}],
            thresholds=_THRESHOLDS,
        )


def test_published_row_must_equal_the_selected_source() -> None:
    selected = _row()
    with pytest.raises(ReleaseRowProofError, match="PUBLISHED_NOT_SELECTED"):
        assert_published_equals_selected(
            code="000001.SZ",
            trade_date="2026-06-11",
            selected_upstream="BAOSTOCK",
            selected_row=selected,
            published_row=_row(close=10.5),
        )
    assert_published_equals_selected(
        code="000001.SZ",
        trade_date="2026-06-11",
        selected_upstream="BAOSTOCK",
        selected_row=selected,
        published_row=_row(),
    )


def test_silent_c_substitution_is_rejected() -> None:
    with pytest.raises(ReleaseRowProofError, match="C_SILENT_SUBSTITUTION"):
        assert_published_equals_selected(
            code="000001.SZ",
            trade_date="2026-06-11",
            selected_upstream="SINA",
            selected_row=_row(),
            published_row=_row(),
        )


def _settled_database(formal: _FormalEnv) -> Path:
    gc.collect()
    database = formal.settings.paths.data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    return database


def _published_version_id(formal: _FormalEnv) -> str:
    database = _settled_database(formal)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as read_only:
        rows = read_only.execute(
            "SELECT version_id FROM dataset_versions"
            " WHERE dataset = 'daily_bar_unadjusted' AND status = 'PUBLISHED'"
        ).fetchall()
    assert len(rows) == 1
    return str(rows[0][0])


def test_row_proof_verifies_every_published_key_against_actual_sources(
    formal: _FormalEnv,
    tmp_path: Path,
) -> None:
    gateways, _modules = _gateways()
    formal.run(
        authorization=formal.token(),
        envelope_path=formal.envelope_path(tmp_path),
        gateways=gateways,
    )
    version_id = _published_version_id(formal)
    proof_rows: list[dict[str, object]] = []
    summary = verify_release_row_proof(
        data_root=formal.settings.paths.data_root,
        version_id=version_id,
        emit=proof_rows.append,
    )
    assert summary["status"] == "VERIFIED"
    assert summary["a_rows"] == summary["rows_verified"]
    assert summary["b_rows"] == summary["rows_verified"]
    assert summary["c_rows"] == 0
    assert summary["selected_upstream"] == "BAOSTOCK"
    assert len(proof_rows) == summary["rows_verified"]
    for row in proof_rows:
        assert set(row) >= {
            "code",
            "trade_date",
            "a_source",
            "b_source",
            "a_fields",
            "b_fields",
            "a_row_sha256",
            "b_row_sha256",
            "deltas",
            "thresholds",
            "passed",
            "selected_source",
            "published_fields",
            "published_row_sha256",
            "difference_report_ids",
            "resolution_run_id",
        }
        assert row["passed"] is True
        assert set(row["a_fields"]) == set(ROW_PROOF_FIELDS)  # type: ignore[arg-type]
    # Strictly read-only: the immutable connection created no sidecar files.
    metadata = formal.settings.paths.data_root / "metadata"
    assert not any(path.name.endswith(("-wal", "-shm", "-journal")) for path in metadata.iterdir())


def test_row_proof_rejects_xtquant_legacy_lineage(
    formal: _FormalEnv,
    tmp_path: Path,
) -> None:
    gateways, _modules = _gateways()
    formal.run(
        authorization=formal.token(),
        envelope_path=formal.envelope_path(tmp_path),
        gateways=gateways,
    )
    version_id = _published_version_id(formal)
    database = formal.settings.paths.data_root / "metadata" / "quantking.db"
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER IF EXISTS ingestion_batches_no_update_terminal")
        row = connection.execute(
            "SELECT rowid, request_params_json FROM ingestion_batches ORDER BY rowid LIMIT 1"
        ).fetchone()
        import json as _json

        params = _json.loads(row[1])
        params["upstream"] = "XTQUANT"
        params["provider"] = "XTQUANT_LEGACY"
        connection.execute(
            "UPDATE ingestion_batches SET request_params_json = ? WHERE rowid = ?",
            (_json.dumps(params), row[0]),
        )
        connection.commit()
    with pytest.raises(ReleaseRowProofError, match="XTQUANT_LINEAGE"):
        verify_release_row_proof(
            data_root=formal.settings.paths.data_root,
            version_id=version_id,
        )


def test_row_proof_rejects_a_tampered_raw_file(
    formal: _FormalEnv,
    tmp_path: Path,
) -> None:
    gateways, _modules = _gateways()
    formal.run(
        authorization=formal.token(),
        envelope_path=formal.envelope_path(tmp_path),
        gateways=gateways,
    )
    version_id = _published_version_id(formal)
    raw_root = formal.settings.paths.data_root / "raw" / "baostock" / "daily_bar_unadjusted"
    batch_directory = sorted(path for path in raw_root.glob("*") if path.is_dir())[0]
    chunk = batch_directory / "responses" / "chunk-00000.json"
    payload = bytearray(chunk.read_bytes())
    payload[-2] ^= 0x01
    chunk.write_bytes(bytes(payload))
    with pytest.raises(ReleaseRowProofError):
        verify_release_row_proof(
            data_root=formal.settings.paths.data_root,
            version_id=version_id,
        )
