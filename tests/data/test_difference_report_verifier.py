"""Red integration tests for S1-04-R2 DifferenceReport verifier."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import utc_now
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.comparison.artifacts import DifferenceReportArtifactWriter
from quantking.data.comparison.comparator import (
    CrossSourceComparator,
    CrossSourceComparisonRequest,
)
from quantking.data.comparison.thresholds import default_thresholds
from quantking.data.comparison.verifier import (
    DifferenceReportVerificationError,
    DifferenceReportVerifier,
)
from quantking.data.models import DatasetKind


def _daily_bar_table(rows: list[dict[str, object]] | None = None) -> pa.Table:
    rows = (
        rows
        if rows is not None
        else [
            {
                "code": "000001.SZ",
                "trade_date": date(2026, 7, 15),
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "pre_close": 10.0,
                "volume": 1_000_000,
                "amount": 10_500_000.0,
            }
        ]
    )
    table = pa.Table.from_pylist(rows)
    descriptor = canonical_schema("daily_bar_unadjusted")
    columns: list[pa.Array] = []
    fields: list[pa.Field] = []
    for field in descriptor:
        columns.append(table.column(field.name).cast(field.type, safe=False))
        fields.append(pa.field(field.name, field.type, nullable=field.nullable))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields))


class TestDifferenceReportVerifier:
    def test_verifier_reads_and_validates_report_structure(self, tmp_path: Path) -> None:
        """Verify a well-formed report passes all checks."""
        primary = _daily_bar_table()
        fallback = _daily_bar_table(
            [
                {
                    "code": "000001.SZ",
                    "trade_date": date(2026, 7, 15),
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.5,
                    "close": 10.8,  # different
                    "pre_close": 10.0,
                    "volume": 1_000_000,
                    "amount": 10_500_000.0,
                }
            ]
        )
        # Compute actual content SHA for both tables.
        from quantking.data.content import canonical_content_sha256

        primary_sha = canonical_content_sha256(DatasetKind.DAILY_BAR_UNADJUSTED, [primary])
        fallback_sha = canonical_content_sha256(DatasetKind.DAILY_BAR_UNADJUSTED, [fallback])

        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256=primary_sha,
            fallback_content_sha256=fallback_sha,
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        outcome = CrossSourceComparator().compare(request=request)
        writer = DifferenceReportArtifactWriter(artifacts_root=tmp_path)
        writer.write(outcome=outcome)

        verifier = DifferenceReportVerifier(artifacts_root=tmp_path)
        verified = verifier.verify(report_id=outcome.report_id)

        assert verified.report_id == outcome.report_id
        assert verified.dataset == "daily_bar_unadjusted"
        assert verified.primary_source == "XTQUANT"
        assert verified.fallback_source == "BAOSTOCK"
        assert verified.total_differences == outcome.total_differences
        assert len(verified.row_differences) == outcome.total_differences

    def test_verifier_rejects_missing_manifest(self, tmp_path: Path) -> None:
        """Missing manifest.json raises verification error."""
        report_id = new_entity_id("data_diff")
        directory = tmp_path / "data-diff" / str(report_id).replace(":", "--")
        directory.mkdir(parents=True)

        verifier = DifferenceReportVerifier(artifacts_root=tmp_path)
        with pytest.raises(DifferenceReportVerificationError, match=r"manifest\.json is missing"):
            verifier.verify(report_id=report_id)

    def test_verifier_rejects_missing_complete_marker(self, tmp_path: Path) -> None:
        """Missing COMPLETE marker raises verification error."""
        from quantking.data.adapters.warehouse.manifest import compute_sha256

        report_id = new_entity_id("data_diff")
        directory = tmp_path / "data-diff" / str(report_id).replace(":", "--")
        directory.mkdir(parents=True)
        manifest = {
            "report_id": str(report_id),
            "dataset": "daily_bar_unadjusted",
            "primary_source": "XTQUANT",
            "fallback_source": "BAOSTOCK",
            "report_sha256": "c" * 64,
        }
        # Compute correct manifest SHA.
        manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8")
        manifest_sha = compute_sha256(manifest_bytes)
        manifest["manifest_sha256"] = manifest_sha
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        verifier = DifferenceReportVerifier(artifacts_root=tmp_path)
        with pytest.raises(DifferenceReportVerificationError, match="COMPLETE marker is missing"):
            verifier.verify(report_id=report_id)

    def test_verifier_rejects_unbound_complete_marker(self, tmp_path: Path) -> None:
        """COMPLETE marker with wrong manifest_sha256 raises verification error."""
        from quantking.data.adapters.warehouse.manifest import compute_sha256

        report_id = new_entity_id("data_diff")
        directory = tmp_path / "data-diff" / str(report_id).replace(":", "--")
        directory.mkdir(parents=True)
        manifest = {
            "report_id": str(report_id),
            "dataset": "daily_bar_unadjusted",
            "primary_source": "XTQUANT",
            "fallback_source": "BAOSTOCK",
            "report_sha256": "c" * 64,
        }
        # Compute correct manifest SHA.
        manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8")
        manifest_sha = compute_sha256(manifest_bytes)
        manifest["manifest_sha256"] = manifest_sha
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        # COMPLETE with wrong SHA.
        (directory / "COMPLETE").write_text(
            json.dumps({"manifest_sha256": "e" * 64}), encoding="utf-8"
        )

        verifier = DifferenceReportVerifier(artifacts_root=tmp_path)
        with pytest.raises(
            DifferenceReportVerificationError, match="COMPLETE marker does not bind to manifest"
        ):
            verifier.verify(report_id=report_id)

    def test_verifier_rejects_missing_row_differences(self, tmp_path: Path) -> None:
        """Missing row_differences.json raises verification error."""
        report_id = new_entity_id("data_diff")
        directory = tmp_path / "data-diff" / str(report_id).replace(":", "--")
        directory.mkdir(parents=True)
        manifest = {
            "report_id": str(report_id),
            "dataset": "daily_bar_unadjusted",
            "primary_source": "XTQUANT",
            "fallback_source": "BAOSTOCK",
            "report_sha256": "c" * 64,
        }
        from quantking.data.adapters.warehouse.manifest import compute_sha256

        manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8")
        manifest_sha = compute_sha256(manifest_bytes)
        manifest["manifest_sha256"] = manifest_sha
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (directory / "COMPLETE").write_text(
            json.dumps({"manifest_sha256": manifest_sha}), encoding="utf-8"
        )

        verifier = DifferenceReportVerifier(artifacts_root=tmp_path)
        with pytest.raises(
            DifferenceReportVerificationError, match=r"row_differences\.json is missing"
        ):
            verifier.verify(report_id=report_id)

    def test_scope_matches_validates_all_tuples_exist(self, tmp_path: Path) -> None:
        """scope_matches returns True only if all (code, date, field) exist."""
        primary = _daily_bar_table()
        fallback = _daily_bar_table(
            [
                {
                    "code": "000001.SZ",
                    "trade_date": date(2026, 7, 15),
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.5,
                    "close": 10.8,  # different
                    "pre_close": 10.0,
                    "volume": 1_000_000,
                    "amount": 10_500_000.0,
                }
            ]
        )
        # Compute actual content SHA.
        from quantking.data.content import canonical_content_sha256

        primary_sha = canonical_content_sha256(DatasetKind.DAILY_BAR_UNADJUSTED, [primary])
        fallback_sha = canonical_content_sha256(DatasetKind.DAILY_BAR_UNADJUSTED, [fallback])

        request = CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version="daily_bar_unadjusted.v1",
            primary_source="XTQUANT",
            fallback_source="BAOSTOCK",
            primary_version_id=new_entity_id("data_version"),
            fallback_version_id=new_entity_id("data_version"),
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256=primary_sha,
            fallback_content_sha256=fallback_sha,
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=(),
            created_at=utc_now(),
        )
        outcome = CrossSourceComparator().compare(request=request)
        writer = DifferenceReportArtifactWriter(artifacts_root=tmp_path)
        writer.write(outcome=outcome)

        verifier = DifferenceReportVerifier(artifacts_root=tmp_path)
        verified = verifier.verify(report_id=outcome.report_id)

        # Actual difference: (000001.SZ, 2026-07-15, close).
        assert verified.scope_matches(
            codes=frozenset({"000001.SZ"}),
            dates=frozenset({"2026-07-15"}),
            fields=frozenset({"close"}),
        )

        # Out-of-scope code.
        assert not verified.scope_matches(
            codes=frozenset({"000999.SZ"}),
            dates=frozenset({"2026-07-15"}),
            fields=frozenset({"close"}),
        )

        # Out-of-scope field.
        assert not verified.scope_matches(
            codes=frozenset({"000001.SZ"}),
            dates=frozenset({"2026-07-15"}),
            fields=frozenset({"open"}),
        )


__all__ = ["TestDifferenceReportVerifier"]
