"""Synthetic-only builders for offline tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from quantking.common.ids import EntityId
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.warehouse.manifest import compute_sha256
from quantking.data.assessment.models import EvidenceKind, SourceCandidateAssessment
from quantking.data.models import DatasetKind
from quantking.data.validation.artifacts import ValidationEvidenceVerifier, ValidationEvidenceWriter
from quantking.data.validation.models import (
    _VERIFICATION_SEAL,
    AssessmentGates,
    ValidationRunEvidence,
    ValidationVersions,
    VerifiedAssessmentEvidence,
)
from quantking.data.validation.samples import load_packaged_fixed_sample


def build_synthetic_evidence_bundle(
    *, root: Path, run_text: str | None = None, run_index: int = 1
) -> tuple[Path, object]:
    if not 1 <= run_index <= 999:
        raise ValueError("run_index must be between 1 and 999")
    payload = ('{"fixture":"' + (run_text or "first") + '"}').encode()
    canonical = b'{"rows":[]}'
    quality = b'{"status":"PASSED"}'
    sample = load_packaged_fixed_sample()
    run = ValidationRunEvidence(
        run_id=EntityId(
            "data_validation",
            UUID(f"00000000-0000-4000-8000-{run_index:012d}"),
        ),
        assessment_id=EntityId("data_assessment", UUID("00000000-0000-4000-8000-000000000002")),
        source="SYNTHETIC_FIXTURE",
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        evidence_kind=EvidenceKind.SYNTHETIC,
        versions=ValidationVersions(
            adapter_version="fixture.v1",
            package_version="not-applicable",
            code_version="s1-07a.v1",
            schema_version="daily-bar-unadjusted.v1",
            rule_version="quality-rules.v1",
            threshold_version="comparison-thresholds.v1",
            sample_spec_version=sample.sample_spec_version,
            config_version="validation.v1",
        ),
        request_sha256=compute_sha256(b'{"fixture":true}'),
        sample_spec_sha256=sample.sample_spec_sha256,
        raw_manifest_relative_path="raw/payload.json",
        raw_manifest_sha256=compute_sha256(payload),
        canonical_content_sha256=compute_sha256(canonical),
        quality_run_id=EntityId(
            "data_quality",
            UUID(f"00000000-0000-4000-9000-{run_index:012d}"),
        ),
        quality_manifest_sha256=compute_sha256(quality),
        difference_report_id=None,
        difference_report_sha256=None,
        gates=AssessmentGates(
            semantic_contract_verified=True,
            schema_unit_verified=True,
            coverage_adequate=True,
            quality_passed=True,
            fixed_sample_differences_adequate=False,
        ),
        started_at=UtcTimestamp(datetime(2026, 7, 28, tzinfo=UTC)),
        ended_at=UtcTimestamp(datetime(2026, 7, 28, 0, 0, 1, tzinfo=UTC)),
        failure=None,
    )
    writer = ValidationEvidenceWriter(validation_root=root)
    directory = writer.write(
        run=run,
        files={
            "canonical/content.json": (canonical, 0),
            "quality/result.json": (quality, 1),
            "raw/payload.json": (payload, 1),
        },
    )
    verified = ValidationEvidenceVerifier(validation_root=root).verify(directory=directory)
    return directory, verified


def verified_synthetic_assessment(
    assessment: SourceCandidateAssessment,
) -> VerifiedAssessmentEvidence:
    """Wrap legacy synthetic fixture evidence for repository tests only."""
    if assessment.evidence_kind is not EvidenceKind.SYNTHETIC:
        raise ValueError("testing helper cannot construct REAL evidence")
    run = ValidationRunEvidence(
        run_id=EntityId("data_validation", UUID("00000000-0000-4000-8000-000000000011")),
        assessment_id=assessment.assessment_id,
        source=assessment.source,
        dataset=assessment.dataset,
        evidence_kind=EvidenceKind.SYNTHETIC,
        versions=ValidationVersions(
            adapter_version="fixture.v1",
            package_version="not-applicable",
            code_version="s1-07a.v1",
            schema_version="fixture-schema.v1",
            rule_version=assessment.rule_version,
            threshold_version="fixture-thresholds.v1",
            sample_spec_version=assessment.sample_spec_version,
            config_version=assessment.assessment_config_version,
        ),
        request_sha256="1" * 64,
        sample_spec_sha256="2" * 64,
        raw_manifest_relative_path="raw/fixture.json",
        raw_manifest_sha256="3" * 64,
        canonical_content_sha256="4" * 64,
        quality_run_id=EntityId("data_quality", UUID("00000000-0000-4000-8000-000000000012")),
        quality_manifest_sha256="5" * 64,
        difference_report_id=None,
        difference_report_sha256=None,
        gates=AssessmentGates(
            assessment.semantic_contract_verified,
            assessment.schema_unit_verified,
            assessment.coverage_adequate,
            assessment.quality_passed,
            assessment.fixed_sample_differences_adequate,
        ),
        started_at=assessment.created_at,
        ended_at=assessment.created_at,
        failure=None,
    )
    return VerifiedAssessmentEvidence(
        assessment=assessment,
        verified_runs=(run,),
        evidence_manifest_sha256=assessment.evidence_sha256,
        evidence_relative_path=assessment.evidence_relative_path,
        _seal=_VERIFICATION_SEAL,
    )


__all__ = ["build_synthetic_evidence_bundle", "verified_synthetic_assessment"]
