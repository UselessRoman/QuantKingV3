"""Assessment gate evaluation over independently verified runs."""

from __future__ import annotations

from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.assessment.models import (
    MIN_PRODUCTION_STABILITY_RUNS,
    AssessmentStatus,
    EvidenceKind,
    SourceCandidateAssessment,
)
from quantking.data.models import DatasetKind
from quantking.data.validation.models import AssessmentGates


def assess_verified_runs(
    *,
    source: str,
    dataset: DatasetKind,
    gates: AssessmentGates,
    verified_run_ids: tuple[EntityId, ...],
    assessment_config_version: str,
    sample_spec_version: str,
    rule_version: str,
    evidence_kind: EvidenceKind = EvidenceKind.REAL,
    assessment_id: EntityId | None = None,
    evidence_relative_path: str = "source-assessments/pending/manifest.json",
    evidence_sha256: str = "0" * 64,
    created_at: UtcTimestamp | None = None,
) -> SourceCandidateAssessment:
    unique_ids = tuple(dict.fromkeys(verified_run_ids))
    if any(item.namespace != "data_validation" for item in unique_ids):
        raise ValueError("verified run IDs must use data_validation namespace")
    ready = (
        evidence_kind is EvidenceKind.REAL
        and gates.passed
        and len(unique_ids) >= MIN_PRODUCTION_STABILITY_RUNS
    )
    reasons: list[str] = []
    if evidence_kind is not EvidenceKind.REAL:
        reasons.append("REAL evidence is required")
    for field, passed in gates.to_dict().items():
        if not passed:
            reasons.append(f"{field} is false")
    if len(unique_ids) < MIN_PRODUCTION_STABILITY_RUNS:
        reasons.append("three independent verified runs are required")
    return SourceCandidateAssessment(
        assessment_id=assessment_id or new_entity_id("data_assessment"),
        source=source,
        dataset=dataset,
        evidence_kind=evidence_kind,
        semantic_contract_verified=gates.semantic_contract_verified,
        schema_unit_verified=gates.schema_unit_verified,
        coverage_adequate=gates.coverage_adequate,
        quality_passed=gates.quality_passed,
        fixed_sample_differences_adequate=gates.fixed_sample_differences_adequate,
        stability_runs=len(unique_ids),
        status=(
            AssessmentStatus.PRODUCTION_READY
            if ready
            else AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION
        ),
        blocking_reason=None if ready else "; ".join(reasons),
        assessment_config_version=assessment_config_version,
        sample_spec_version=sample_spec_version,
        rule_version=rule_version,
        evidence_relative_path=evidence_relative_path,
        evidence_sha256=evidence_sha256,
        created_at=created_at or utc_now(),
    )


__all__ = ["assess_verified_runs"]
