"""S1-04-R3 red tests for strict DifferenceReport artifact verification."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import utc_now
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.warehouse.manifest import compute_sha256
from quantking.data.comparison.artifacts import DifferenceReportArtifactWriter
from quantking.data.comparison.comparator import (
    CrossSourceComparator,
    CrossSourceComparisonRequest,
    RowClassification,
)
from quantking.data.comparison.thresholds import default_thresholds
from quantking.data.comparison.verifier import (
    DifferenceReportVerificationError,
    DifferenceReportVerifier,
)
from quantking.data.content import canonical_content_sha256
from quantking.data.models import DatasetKind


def _table(rows: list[dict[str, object]]) -> pa.Table:
    raw = pa.Table.from_pylist(rows)
    schema = canonical_schema("daily_bar_unadjusted")
    return pa.Table.from_arrays(
        [raw.column(field.name).cast(field.type, safe=False) for field in schema],
        schema=pa.schema(
            [pa.field(field.name, field.type, nullable=field.nullable) for field in schema]
        ),
    )


def _rows(close: float = 10.5) -> list[dict[str, object]]:
    return [
        {
            "code": "000001.SZ",
            "trade_date": date(2026, 7, 15),
            "open": 10.0,
            "high": 11.0,
            "low": 9.5,
            "close": close,
            "pre_close": 10.0,
            "volume": 1_000_000,
            "amount": 10_500_000.0,
        }
    ]


def _write_report(tmp_path: Path, *, fallback_rows: list[dict[str, object]] | None = None):
    primary = _table(_rows())
    fallback = _table(fallback_rows or _rows(close=10.8))
    request = CrossSourceComparisonRequest(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        schema_version="daily_bar_unadjusted.v1",
        primary_source="EASTMONEY",
        fallback_source="TENCENT",
        primary_version_id=new_entity_id("data_version"),
        fallback_version_id=new_entity_id("data_version"),
        primary_batch_id=new_entity_id("data_batch"),
        fallback_batch_id=new_entity_id("data_batch"),
        primary_content_sha256=canonical_content_sha256(
            DatasetKind.DAILY_BAR_UNADJUSTED, [primary]
        ),
        fallback_content_sha256=canonical_content_sha256(
            DatasetKind.DAILY_BAR_UNADJUSTED, [fallback]
        ),
        primary_quality_run_id=new_entity_id("data_quality"),
        fallback_quality_run_id=new_entity_id("data_quality"),
        primary_table=primary,
        fallback_table=fallback,
        thresholds=default_thresholds(),
        sample_spec=("fixed:000001.SZ:2026-07-15",),
        created_at=utc_now(),
    )
    outcome = CrossSourceComparator().compare(request=request)
    directory = DifferenceReportArtifactWriter(artifacts_root=tmp_path).write(outcome=outcome)
    return outcome, directory


def test_strict_verifier_returns_exact_difference_units_and_manifest_credentials(
    tmp_path: Path,
) -> None:
    outcome, directory = _write_report(tmp_path)

    verified = DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=outcome.report_id)

    assert verified.report_id == outcome.report_id
    assert verified.report_relative_path == directory.relative_to(tmp_path).as_posix()
    assert verified.manifest_sha256
    assert verified.report_sha256 == outcome.report_sha256
    assert verified.primary_version_id == outcome.primary_version_id
    assert verified.fallback_version_id == outcome.fallback_version_id
    assert verified.primary_batch_id == outcome.primary_batch_id
    assert verified.fallback_batch_id == outcome.fallback_batch_id
    assert verified.primary_quality_run_id == outcome.primary_quality_run_id
    assert verified.fallback_quality_run_id == outcome.fallback_quality_run_id
    assert verified.sample_spec_sha256 == compute_sha256(
        json.dumps(["fixed:000001.SZ:2026-07-15"], separators=(",", ":"), sort_keys=True).encode()
    )
    assert [(unit.primary_key, unit.field, unit.classification) for unit in verified.units] == [
        ({"code": "000001.SZ", "trade_date": "2026-07-15"}, "close", RowClassification.BOTH)
    ]


def test_strict_verifier_rejects_extra_files_and_row_evidence_tamper(tmp_path: Path) -> None:
    outcome, directory = _write_report(tmp_path)
    (directory / "unexpected.json").write_text("{}", encoding="utf-8")

    with pytest.raises(DifferenceReportVerificationError, match="file set"):
        DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=outcome.report_id)

    (directory / "unexpected.json").unlink()
    rows_path = directory / "row_differences.json"
    payload = json.loads(rows_path.read_text(encoding="utf-8"))
    payload["rows"][0]["field"] = "open"
    rows_path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True), encoding="utf-8"
    )

    with pytest.raises(DifferenceReportVerificationError, match=r"report_sha256|row evidence"):
        DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=outcome.report_id)


def test_strict_verifier_rejects_noncanonical_manifest_bytes(tmp_path: Path) -> None:
    outcome, directory = _write_report(tmp_path)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(DifferenceReportVerificationError, match="canonical"):
        DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=outcome.report_id)


def test_fallback_only_scope_requires_explicit_row_unit(tmp_path: Path) -> None:
    fallback_rows = [
        *_rows(close=10.8),
        {
            "code": "000002.SZ",
            "trade_date": date(2026, 7, 15),
            "open": 20.0,
            "high": 21.0,
            "low": 19.5,
            "close": 20.5,
            "pre_close": 20.0,
            "volume": 2_000_000,
            "amount": 41_000_000.0,
        },
    ]
    outcome, _ = _write_report(tmp_path, fallback_rows=fallback_rows)

    verified = DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=outcome.report_id)

    assert not verified.scope_matches(
        codes=frozenset({"000002.SZ"}),
        dates=frozenset({"2026-07-15"}),
        fields=frozenset({"close"}),
    )
    assert verified.fallback_only_rows == frozenset(
        {(("code", "000002.SZ"), ("trade_date", "2026-07-15"))}
    )
