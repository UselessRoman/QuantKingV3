from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal, getcontext
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.ids import EntityId, new_entity_id, parse_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.canonical_schema import canonical_schema, ensure_sorted
from quantking.data.comparison.artifacts import DifferenceReportArtifactWriter
from quantking.data.comparison.comparator import (
    CrossSourceComparator,
    CrossSourceComparisonOutcome,
    CrossSourceComparisonRequest,
    RowClassification,
    RowDifference,
)
from quantking.data.comparison.thresholds import (
    K3_CANDIDATE_RULE_VERSION,
    THRESHOLD_RULE_VERSION,
    ThresholdSpec,
    canonical_decimal,
    default_thresholds,
    k3_evidence_candidate_thresholds,
    k3_threshold_candidate_contract,
)
from quantking.data.comparison.verifier import (
    DifferenceReportVerificationError,
    DifferenceReportVerifier,
)
from quantking.data.content import canonical_content_sha256
from quantking.data.models import DatasetKind, DataUpstream

_CREATED = UtcTimestamp.from_iso("2026-08-12T10:00:00Z")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _table(close: float) -> pa.Table:
    return ensure_sorted(
        DatasetKind.DAILY_BAR_UNADJUSTED.value,
        pa.Table.from_pylist(
            [
                {
                    "code": "600000.SH",
                    "trade_date": date(2026, 8, 7),
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.5,
                    "close": close,
                    "pre_close": 10.0,
                    "volume": 1_000_000,
                    "amount": 10_500_000.0,
                }
            ],
            schema=canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value),
        ),
    )


def _candidate_request() -> CrossSourceComparisonRequest:
    primary = _table(10.5)
    fallback = _table(10.6)
    shared_version = new_entity_id("data_version")
    return CrossSourceComparisonRequest(
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        schema_version="daily_bar_unadjusted.v1",
        primary_source=DataUpstream.BAOSTOCK,
        fallback_source=DataUpstream.EASTMONEY,
        primary_version_id=shared_version,
        fallback_version_id=shared_version,
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
        thresholds=k3_evidence_candidate_thresholds(),
        sample_spec=("K3_DECIMAL_EVIDENCE_ONLY",),
        created_at=_CREATED,
        comparison_primary_keys=(("600000.SH", date(2026, 8, 7)),),
    )


def test_k1_rule_remains_active_default_zero_tolerance_and_legacy_golden_bytes() -> None:
    assert default_thresholds().rule_version == THRESHOLD_RULE_VERSION
    assert THRESHOLD_RULE_VERSION == "comparison-thresholds.daily-bar-unadjusted.k1.v1"
    row = RowDifference(
        primary_key={"code": "600000.SH", "trade_date": "2026-08-07"},
        field="close",
        classification=RowClassification.BOTH,
        primary_value=10.5,
        fallback_value=10.6,
        absolute_difference=Decimal("0.1"),
        relative_difference=Decimal("0.1") / Decimal("10.6"),
        threshold=ThresholdSpec(Decimal("0"), Decimal("0")),
    )
    legacy_bytes = _canonical(row.to_dict())
    assert hashlib.sha256(legacy_bytes).hexdigest() == (
        "05b9bed712dd3c9f072727262c17f706b188f47e1d5391bba638a61ea05363ff"
    )
    assert b'"absolute_difference":0.09999999999999964' in legacy_bytes
    assert b'"threshold":{"absolute":0.0,"relative":0.0}' in legacy_bytes


def test_candidate_is_zero_only_inactive_and_never_replaces_default() -> None:
    candidate = k3_threshold_candidate_contract()
    assert candidate.active is False
    assert candidate.evidence_status == "REPRESENTATIVE_REAL_EVIDENCE_MISSING"
    assert candidate.thresholds.rule_version == K3_CANDIDATE_RULE_VERSION
    assert candidate.thresholds.rule_version != default_thresholds().rule_version
    assert all(
        spec.absolute == 0 and spec.relative == 0
        for spec in candidate.thresholds.for_dataset(DatasetKind.DAILY_BAR_UNADJUSTED).values()
    )


def test_decimal_evidence_and_sha_ignore_mutated_global_decimal_context() -> None:
    request = _candidate_request()
    outcomes: list[CrossSourceComparisonOutcome] = []
    original = getcontext().copy()
    try:
        for precision in (6, 50):
            getcontext().prec = precision
            outcomes.append(CrossSourceComparator().compare(request=request))
    finally:
        getcontext().prec = original.prec
        getcontext().rounding = original.rounding
    assert outcomes[0].report_sha256 == outcomes[1].report_sha256
    first = outcomes[0].row_differences[0].to_dict(decimal_strings=True)
    second = outcomes[1].row_differences[0].to_dict(decimal_strings=True)
    assert first == second
    assert first["absolute_difference"] == "0.1"
    assert first["relative_difference"] == (
        "0.0094339622641509433962264150943396226415094339622642"
    )
    assert canonical_decimal(Decimal("1E+3")) == "1000"
    assert canonical_decimal(Decimal("50.000")) == "50"


def test_k3_v3_artifact_round_trip_uses_canonical_decimal_strings(tmp_path: Path) -> None:
    outcome = CrossSourceComparator().compare(request=_candidate_request())
    directory = DifferenceReportArtifactWriter(artifacts_root=tmp_path).write(outcome=outcome)
    manifest = json.loads((directory / "manifest.json").read_bytes())
    rows = json.loads((directory / "row_differences.json").read_bytes())["rows"]
    assert manifest["manifest_schema_version"] == "difference-report-manifest.k3.v3"
    assert manifest["threshold_rule_version"] == K3_CANDIDATE_RULE_VERSION
    assert manifest["unit_policy_versions"] == {
        "primary": "baostock-0.9.3-returned-units.v1",
        "fallback": "akshare-1.18.83-returned-units.v1",
    }
    assert rows[0]["absolute_difference"] == "0.1"
    assert rows[0]["threshold"] == {"absolute": "0", "relative": "0"}
    verified = DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=outcome.report_id)
    assert verified.report_sha256 == outcome.report_sha256


def _resign_unit_policy(directory: Path, value: object) -> EntityId:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["unit_policy_versions"] = value
    manifest.pop("manifest_sha256")
    digest = hashlib.sha256(_canonical(manifest)).hexdigest()
    manifest_path.write_bytes(_canonical({**manifest, "manifest_sha256": digest}))
    (directory / "COMPLETE").write_bytes(_canonical({"manifest_sha256": digest}))
    return parse_entity_id(str(manifest["report_id"]))


@pytest.mark.parametrize(
    "value",
    [
        {"primary": "unverified", "fallback": "unverified"},
        {"primary": "baostock-0.9.3-returned-units.v1"},
        "baostock-0.9.3-returned-units.v1",
    ],
)
def test_k3_unit_policy_versions_tamper_is_rejected_after_resign(
    tmp_path: Path, value: object
) -> None:
    outcome = CrossSourceComparator().compare(request=_candidate_request())
    directory = DifferenceReportArtifactWriter(artifacts_root=tmp_path).write(outcome=outcome)
    report_id = _resign_unit_policy(directory, value)
    with pytest.raises(DifferenceReportVerificationError, match="unit policy"):
        DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=report_id)


def _resign_tampered_threshold(directory: Path) -> EntityId:
    rows_path = directory / "row_differences.json"
    rows_payload = json.loads(rows_path.read_bytes())
    rows_payload["rows"][0]["threshold"]["absolute"] = "50"
    rows_bytes = _canonical(rows_payload)
    rows_path.write_bytes(rows_bytes)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["row_differences_file_sha256"] = hashlib.sha256(rows_bytes).hexdigest()
    manifest["row_differences_file_size"] = len(rows_bytes)
    request = {
        "dataset": manifest["dataset"],
        "schema_version": manifest["schema_version"],
        "primary_source": manifest["primary_source"],
        "fallback_source": manifest["fallback_source"],
        "primary_content_sha256": manifest["primary_content_sha256"],
        "fallback_content_sha256": manifest["fallback_content_sha256"],
        "threshold_rule_version": manifest["threshold_rule_version"],
        "sample_spec": manifest["sample_spec"],
        "comparison_primary_keys": manifest["comparison_primary_keys"],
        "primary_empty_result": manifest["primary_empty_result"],
        "fallback_empty_result": manifest["fallback_empty_result"],
    }
    report = {
        "request": request,
        "total_compared": manifest["total_compared"],
        "total_differences": manifest["total_differences"],
        "field_differences": manifest["field_differences"],
        "row_differences": rows_payload["rows"],
    }
    manifest["report_sha256"] = hashlib.sha256(_canonical(report)).hexdigest()
    manifest.pop("manifest_sha256")
    digest = hashlib.sha256(_canonical(manifest)).hexdigest()
    manifest_path.write_bytes(_canonical({**manifest, "manifest_sha256": digest}))
    (directory / "COMPLETE").write_bytes(_canonical({"manifest_sha256": digest}))
    return parse_entity_id(str(manifest["report_id"]))


def test_v3_threshold_tamper_is_rejected_even_after_full_resign(tmp_path: Path) -> None:
    outcome = CrossSourceComparator().compare(request=_candidate_request())
    directory = DifferenceReportArtifactWriter(artifacts_root=tmp_path).write(outcome=outcome)
    report_id = _resign_tampered_threshold(directory)
    with pytest.raises(DifferenceReportVerificationError, match="threshold evidence mismatch"):
        DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=report_id)


@pytest.mark.parametrize("value", ["1.0", "01", "1e0", "NaN", "Infinity", "-1"])
def test_noncanonical_or_invalid_decimal_evidence_is_rejected(
    tmp_path: Path,
    value: str,
) -> None:
    outcome = CrossSourceComparator().compare(request=_candidate_request())
    directory = DifferenceReportArtifactWriter(artifacts_root=tmp_path).write(outcome=outcome)
    rows_path = directory / "row_differences.json"
    payload = json.loads(rows_path.read_bytes())
    payload["rows"][0]["absolute_difference"] = value
    rows_path.write_bytes(_canonical(payload))
    with pytest.raises(DifferenceReportVerificationError):
        DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=outcome.report_id)
