from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.canonical_schema import canonical_schema, ensure_sorted
from quantking.data.comparison.artifacts import DifferenceReportArtifactWriter
from quantking.data.comparison.comparator import (
    CrossSourceComparator,
    CrossSourceComparisonRequest,
)
from quantking.data.comparison.thresholds import THRESHOLD_RULE_VERSION, default_thresholds
from quantking.data.comparison.verifier import (
    DifferenceReportVerificationError,
    DifferenceReportVerifier,
)
from quantking.data.content import canonical_content_sha256
from quantking.data.models import (
    DatasetKind,
    DataUpstream,
    EmptyResultInfo,
    LegalEmptyClassification,
)
from quantking.data.schemas import descriptor_for

_CREATED = UtcTimestamp.from_iso("2026-08-09T10:00:00Z")
_EMPTY = EmptyResultInfo(
    classification=LegalEmptyClassification.NO_TRADING_SESSION,
    reason="explicit no trading session",
)


def _row(trade_date: date, close: float) -> dict[str, object]:
    return {
        "code": "600000.SH",
        "trade_date": trade_date,
        "open": 10.0,
        "high": 11.0,
        "low": 9.5,
        "close": close,
        "pre_close": 10.0,
        "volume": 1_000_000,
        "amount": 10_500_000.0,
    }


def _table(rows: list[dict[str, object]]) -> pa.Table:
    return ensure_sorted(
        DatasetKind.DAILY_BAR_UNADJUSTED.value,
        pa.Table.from_pylist(
            rows,
            schema=canonical_schema(DatasetKind.DAILY_BAR_UNADJUSTED.value),
        ),
    )


def _outcome(*, shared: bool = True) -> object:
    first = date(2026, 8, 6)
    target = date(2026, 8, 7)
    primary = _table([_row(first, 10.4), _row(target, 10.5)])
    fallback = _table([_row(first, 10.4), _row(target, 10.6)])
    primary_version = new_entity_id("data_version")
    fallback_version = primary_version if shared else new_entity_id("data_version")
    return CrossSourceComparator().compare(
        request=CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version=descriptor_for(DatasetKind.DAILY_BAR_UNADJUSTED.value).schema_version,
            primary_source=DataUpstream.EASTMONEY,
            fallback_source=DataUpstream.TENCENT,
            primary_version_id=primary_version,
            fallback_version_id=fallback_version,
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
            sample_spec=("K2_AFFECTED_SCOPE",),
            created_at=_CREATED,
            comparison_primary_keys=(("600000.SH", target),) if shared else None,
        )
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _resign(
    directory: Path,
    mutation: dict[str, object],
    *,
    recompute_report_sha: bool = False,
) -> None:
    manifest = json.loads((directory / "manifest.json").read_bytes())
    manifest.update(mutation)
    if recompute_report_sha:
        rows = json.loads((directory / "row_differences.json").read_bytes())["rows"]
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
            "row_differences": rows,
        }
        manifest["report_sha256"] = hashlib.sha256(_canonical(report)).hexdigest()
    manifest.pop("manifest_sha256")
    digest = hashlib.sha256(_canonical(manifest)).hexdigest()
    (directory / "manifest.json").write_bytes(_canonical({**manifest, "manifest_sha256": digest}))
    (directory / "COMPLETE").write_bytes(_canonical({"manifest_sha256": digest}))


def _empty_outcome(*, fallback_has_row: bool) -> object:
    target = date(2026, 8, 7)
    primary = _table([])
    fallback = _table([_row(target, 10.5)] if fallback_has_row else [])
    version = new_entity_id("data_version")
    return CrossSourceComparator().compare(
        request=CrossSourceComparisonRequest(
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            schema_version=descriptor_for(DatasetKind.DAILY_BAR_UNADJUSTED.value).schema_version,
            primary_source=DataUpstream.EASTMONEY,
            fallback_source=DataUpstream.TENCENT,
            primary_version_id=version,
            fallback_version_id=version,
            primary_batch_id=new_entity_id("data_batch"),
            fallback_batch_id=new_entity_id("data_batch"),
            primary_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED,
                [primary],
                empty_result=_EMPTY,
            ),
            fallback_content_sha256=canonical_content_sha256(
                DatasetKind.DAILY_BAR_UNADJUSTED,
                [fallback],
                empty_result=None if fallback_has_row else _EMPTY,
            ),
            primary_quality_run_id=new_entity_id("data_quality"),
            fallback_quality_run_id=new_entity_id("data_quality"),
            primary_table=primary,
            fallback_table=fallback,
            thresholds=default_thresholds(),
            sample_spec=("K2_AFFECTED_SCOPE",),
            created_at=_CREATED,
            comparison_primary_keys=(("600000.SH", target),),
            primary_empty_result=_EMPTY,
            fallback_empty_result=None if fallback_has_row else _EMPTY,
        )
    )


def test_k2_v2_roundtrip_binds_shared_candidate_and_exact_scope(tmp_path: Path) -> None:
    outcome = _outcome()
    DifferenceReportArtifactWriter(artifacts_root=tmp_path).write(outcome=outcome)

    verified = DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=outcome.report_id)

    assert verified.manifest_schema_version == "difference-report-manifest.k2.v2"
    assert verified.binding_mode == "SHARED_RESOLUTION_CANDIDATE"
    assert verified.primary_version_id == verified.fallback_version_id
    assert verified.comparison_primary_keys == (("600000.SH", "2026-08-07"),)
    assert verified.total_compared == 1
    assert verified.total_differences == 1


@pytest.mark.parametrize("fallback_has_row", [True, False])
def test_k2_comparator_writer_verifier_bind_legal_empty_sides(
    tmp_path: Path,
    fallback_has_row: bool,
) -> None:
    outcome = _empty_outcome(fallback_has_row=fallback_has_row)
    DifferenceReportArtifactWriter(artifacts_root=tmp_path).write(outcome=outcome)

    verified = DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=outcome.report_id)

    assert verified.primary_empty_result == {
        "classification": "NO_TRADING_SESSION",
        "reason": "explicit no trading session",
    }
    assert (verified.fallback_empty_result is None) is fallback_has_row
    assert verified.total_differences == (1 if fallback_has_row else 0)


@pytest.mark.parametrize(
    "mutation",
    [
        {"binding_mode": "DISTINCT_PUBLISHED_VERSIONS"},
        {"fallback_version_id": str(new_entity_id("data_version"))},
        {"dataset": DatasetKind.ADJUSTMENT_FACTOR.value},
        {"threshold_rule_version": "comparison-thresholds.v0"},
    ],
)
def test_resigned_k2_binding_tampering_is_rejected(
    tmp_path: Path,
    mutation: dict[str, object],
) -> None:
    outcome = _outcome()
    directory = DifferenceReportArtifactWriter(artifacts_root=tmp_path).write(outcome=outcome)
    _resign(directory, mutation)

    with pytest.raises(DifferenceReportVerificationError):
        DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=outcome.report_id)


@pytest.mark.parametrize(
    "scope",
    [
        [["600000.SH"]],
        [["BAD", "2026-08-07"]],
        [["600000.SH", "2026-8-7"]],
        [["600000.SH", "2026-08-07"], ["600000.SH", "2026-08-07"]],
        [["600000.SH", "2026-08-08"], ["600000.SH", "2026-08-07"]],
        [["600000.SH", "2026-08-06"]],
    ],
)
def test_resigned_k2_comparison_scope_tampering_is_rejected(
    tmp_path: Path,
    scope: list[list[str]],
) -> None:
    outcome = _outcome()
    directory = DifferenceReportArtifactWriter(artifacts_root=tmp_path).write(outcome=outcome)
    _resign(
        directory,
        {"comparison_primary_keys": scope},
        recompute_report_sha=True,
    )

    with pytest.raises(DifferenceReportVerificationError):
        DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=outcome.report_id)


def test_legacy_v1_remains_readable_but_cannot_claim_shared_version(tmp_path: Path) -> None:
    outcome = _outcome(shared=False)
    directory = DifferenceReportArtifactWriter(artifacts_root=tmp_path).write(outcome=outcome)
    verified = DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=outcome.report_id)
    assert verified.manifest_schema_version == "difference-report-manifest.v1"
    assert verified.binding_mode is None
    assert verified.threshold_rule_version == THRESHOLD_RULE_VERSION

    _resign(directory, {"fallback_version_id": str(outcome.primary_version_id)})
    with pytest.raises(DifferenceReportVerificationError, match="must differ"):
        DifferenceReportVerifier(artifacts_root=tmp_path).verify(report_id=outcome.report_id)
