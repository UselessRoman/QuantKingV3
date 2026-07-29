"""S1-07A offline validation contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import TradingDate, utc_now
from quantking.data.assessment.models import EvidenceKind
from quantking.data.models import DatasetKind


def test_repository_fixed_sample_is_canonical_and_covers_required_markets() -> None:
    from quantking.data.validation.samples import load_packaged_fixed_sample

    spec = load_packaged_fixed_sample()
    assert spec.sample_spec_version == "s1-07.fixed-sample.v2"
    assert spec.sample_spec_sha256 == spec.compute_sha256()
    assert json.loads(spec.to_canonical_json())["sample_spec_sha256"] == spec.sample_spec_sha256
    assert {case.dataset for case in spec.cases} == set(DatasetKind)
    assert {case.exchange for case in spec.cases} >= {"SH", "SZ", "BJ"}
    assert {
        (case.exchange, case.dataset) for case in spec.cases if case.purpose.value == "BENCHMARK"
    } >= {
        ("SH", DatasetKind.BENCHMARK_INDEX_DAILY),
        ("SZ", DatasetKind.BENCHMARK_INDEX_DAILY),
    }
    assert all(case.start_date.value <= case.end_date.value for case in spec.cases)
    assert all(case.reference.reference_version for case in spec.cases)


@pytest.mark.parametrize(
    ("code", "exchange"),
    [("000001.SZ", "SH"), ("600000.SH", "SZ"), ("430047.BJ", "SH")],
)
def test_fixed_sample_rejects_code_exchange_mismatch(code: str, exchange: str) -> None:
    from quantking.data.validation.samples import (
        FixedSampleCase,
        SamplePurpose,
        SampleReference,
    )

    with pytest.raises(ValueError, match="exchange"):
        FixedSampleCase(
            case_id="mismatch",
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            code=code,
            exchange=exchange,
            start_date=TradingDate.from_iso("2024-01-02"),
            end_date=TradingDate.from_iso("2024-01-03"),
            fields=("open", "close"),
            expected_units={"open": "CNY", "close": "CNY"},
            purpose=SamplePurpose.NORMAL,
            applicable_sources=("XTQUANT",),
            reference=SampleReference(
                authority="repository contract",
                locator="docs/stage1/DATA_CONTRACTS.md",
                reference_version="git:44d268b",
                reviewed_at=TradingDate.from_iso("2026-07-28"),
            ),
        )


def test_real_run_requires_real_evidence_and_distinct_stability_runs() -> None:
    from quantking.data.validation.models import (
        AssessmentGates,
        RealSourceValidationRun,
        ValidationVersions,
    )

    versions = ValidationVersions(
        adapter_version="adapter.v1",
        package_version="1.0.0",
        code_version="code.v1",
        schema_version="daily-bar-unadjusted.v1",
        rule_version="quality-rules.v1",
        threshold_version="comparison-thresholds.v1",
        sample_spec_version="s1-07.fixed-sample.v1",
        config_version="validation.v1",
    )
    with pytest.raises((TypeError, ValueError)):
        RealSourceValidationRun(
            run_id=new_entity_id("data_validation"),
            assessment_id=new_entity_id("data_assessment"),
            source="BAOSTOCK",
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            evidence_kind=EvidenceKind.SYNTHETIC,
            versions=versions,
            request_sha256="a" * 64,
            sample_spec_sha256="b" * 64,
            raw_manifest_relative_path="raw/manifest.json",
            raw_manifest_sha256="c" * 64,
            canonical_content_sha256="d" * 64,
            quality_run_id=new_entity_id("data_quality"),
            quality_manifest_sha256="e" * 64,
            difference_report_id=None,
            difference_report_sha256=None,
            gates=AssessmentGates.all_passed(),
            started_at=utc_now(),
            ended_at=utc_now(),
            failure=None,
        )


def test_assessment_gate_requires_three_distinct_verified_runs() -> None:
    from quantking.data.validation.models import AssessmentGates
    from quantking.data.validation.service import assess_verified_runs

    with pytest.raises(TypeError):
        assess_verified_runs(  # type: ignore[call-arg]
            source="AKSHARE",
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            gates=AssessmentGates.all_passed(),
            verified_run_ids=(
                new_entity_id("data_validation"),
                new_entity_id("data_validation"),
            ),
            assessment_config_version="validation.v1",
            sample_spec_version="s1-07.fixed-sample.v1",
            rule_version="quality-rules.v1",
        )


def test_plan_is_offline_and_does_not_import_vendor_sdks(tmp_path: Path) -> None:
    from quantking.data.validation.cli import build_plan

    before = set(sys.modules)
    plan = build_plan(
        source="XTQUANT",
        dataset=DatasetKind.EXCHANGE_CALENDAR,
        output_root=tmp_path,
    )
    assert plan.network_access is False
    assert plan.sdk_loaded is False
    assert not ({"xtquant", "baostock", "akshare", "tushare"} & (set(sys.modules) - before))


def test_execute_requires_exact_real_acknowledgement(tmp_path: Path) -> None:
    from quantking.data.validation.cli import REAL_EXECUTION_ACK, execute

    with pytest.raises(ValueError, match="acknowledgement"):
        execute(
            source="XTQUANT",
            dataset=DatasetKind.EXCHANGE_CALENDAR,
            output_root=tmp_path,
            acknowledgement=f"{REAL_EXECUTION_ACK}-wrong",
        )
