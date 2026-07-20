"""Red tests for fallback-source candidate assessment (S1-04B).

The candidate assessment records per-source x dataset evidence without
allowing synthetic data to mark any source as production-ready. The
production matrix must remain BLOCKED_EXTERNAL_VALIDATION / UNVERIFIED.
No auto-switch configuration may exist.
"""

from __future__ import annotations

import pytest

from quantking.common.ids import new_entity_id
from quantking.common.time import utc_now
from quantking.data.models import DatasetKind


class TestSourceCandidateAssessment:
    def test_assessment_records_synthetic_evidence_kind(self) -> None:
        from quantking.data.assessment.models import (
            AssessmentStatus,
            EvidenceKind,
            SourceCandidateAssessment,
        )

        assessment = SourceCandidateAssessment(
            assessment_id=new_entity_id("data_assessment"),
            source="BAOSTOCK",
            dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
            evidence_kind=EvidenceKind.SYNTHETIC,
            semantic_contract_verified=True,
            schema_unit_verified=True,
            coverage_adequate=True,
            quality_passed=True,
            fixed_sample_differences_adequate=True,
            stability_runs=3,
            status=AssessmentStatus.UNVERIFIED,
            blocking_reason="synthetic data cannot certify production readiness",
            assessment_config_version="assessment.v1",
            sample_spec_version="sample.v1",
            rule_version="comparison-thresholds.v1",
            evidence_relative_path="assessments/data-assessment/test.json",
            evidence_sha256="a" * 64,
            created_at=utc_now(),
        )
        assert assessment.evidence_kind is EvidenceKind.SYNTHETIC
        assert assessment.status is AssessmentStatus.UNVERIFIED
        assert assessment.blocking_reason

    def test_assessment_blocks_synthetic_evidence_from_production_status(self) -> None:
        from quantking.data.assessment.models import (
            AssessmentStatus,
            EvidenceKind,
            SourceCandidateAssessment,
        )

        # A synthetic-only assessment cannot have PRODUCTION_READY status.
        with pytest.raises((TypeError, ValueError)):
            SourceCandidateAssessment(
                assessment_id=new_entity_id("data_assessment"),
                source="BAOSTOCK",
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                evidence_kind=EvidenceKind.SYNTHETIC,
                semantic_contract_verified=True,
                schema_unit_verified=True,
                coverage_adequate=True,
                quality_passed=True,
                fixed_sample_differences_adequate=True,
                stability_runs=3,
                status=AssessmentStatus.PRODUCTION_READY,  # type: ignore[arg-type]
                blocking_reason=None,
                assessment_config_version="assessment.v1",
                sample_spec_version="sample.v1",
                rule_version="comparison-thresholds.v1",
                evidence_relative_path="assessments/data-assessment/test.json",
                evidence_sha256="a" * 64,
                created_at=utc_now(),
            )

    def test_unverified_status_requires_blocking_reason(self) -> None:
        from quantking.data.assessment.models import (
            AssessmentStatus,
            EvidenceKind,
            SourceCandidateAssessment,
        )

        with pytest.raises((TypeError, ValueError)):
            SourceCandidateAssessment(
                assessment_id=new_entity_id("data_assessment"),
                source="AKSHARE",
                dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
                evidence_kind=EvidenceKind.SYNTHETIC,
                semantic_contract_verified=True,
                schema_unit_verified=True,
                coverage_adequate=False,
                quality_passed=False,
                fixed_sample_differences_adequate=False,
                stability_runs=0,
                status=AssessmentStatus.UNVERIFIED,
                blocking_reason=None,  # invalid: UNVERIFIED requires reason
                assessment_config_version="assessment.v1",
                sample_spec_version="sample.v1",
                rule_version="comparison-thresholds.v1",
                evidence_relative_path="assessments/data-assessment/test.json",
                evidence_sha256="a" * 64,
                created_at=utc_now(),
            )


class TestSourceMatrix:
    def test_default_matrix_lists_three_fallback_sources_as_unverified(self) -> None:
        from quantking.data.assessment.matrix import default_source_matrix

        matrix = default_source_matrix()
        entries = {entry.source: entry for entry in matrix.entries}
        assert "BAOSTOCK" in entries
        assert "AKSHARE" in entries
        assert "TUSHARE" in entries
        for source in ("BAOSTOCK", "AKSHARE", "TUSHARE"):
            entry = entries[source]
            assert entry.status.value in ("BLOCKED_EXTERNAL_VALIDATION", "UNVERIFIED")
            assert entry.blocking_reason is not None

    def test_default_matrix_does_not_auto_switch_to_fallback(self) -> None:
        from quantking.data.assessment.matrix import default_source_matrix

        matrix = default_source_matrix()
        # No entry may declare auto-switch enabled.
        for entry in matrix.entries:
            assert not getattr(entry, "auto_switch", False)

    def test_default_matrix_marks_xtquant_as_primary(self) -> None:
        from quantking.data.assessment.matrix import default_source_matrix

        matrix = default_source_matrix()
        entries = {entry.source: entry for entry in matrix.entries}
        assert "XTQUANT" in entries
        xtquant = entries["XTQUANT"]
        # XTQuant is the primary source; no fallback role.
        assert xtquant.role.value == "PRIMARY"


__all__ = ["TestSourceCandidateAssessment", "TestSourceMatrix"]
