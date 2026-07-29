"""Deterministic assessment derivation from deep-verified runs only."""

from __future__ import annotations

from collections import Counter

from quantking.common.time import UtcTimestamp
from quantking.data.assessment.models import (
    MIN_PRODUCTION_STABILITY_RUNS,
    AssessmentStatus,
    EvidenceKind,
    SourceCandidateAssessment,
)
from quantking.data.validation.models import AssessmentGates, VerifiedValidationRun
from quantking.data.validation.samples import SampleEligibility


def aggregate_gates(runs: tuple[VerifiedValidationRun, ...]) -> AssessmentGates:
    if not runs:
        raise ValueError("at least one verified run is required")
    return AssessmentGates(
        semantic_contract_verified=all(run.gates.semantic_contract_verified for run in runs),
        schema_unit_verified=all(run.gates.schema_unit_verified for run in runs),
        coverage_adequate=all(run.gates.coverage_adequate for run in runs),
        quality_passed=all(run.gates.quality_passed for run in runs),
        fixed_sample_differences_adequate=all(
            run.gates.fixed_sample_differences_adequate for run in runs
        ),
    )


def _stable_count(runs: tuple[VerifiedValidationRun, ...]) -> tuple[int, str | None]:
    fingerprints = Counter(run.run_fingerprint for run in runs)
    common, count = fingerprints.most_common(1)[0]
    eligible = [run for run in runs if run.run_fingerprint == common]
    independent = min(
        len({run.run_id for run in eligible}),
        len({run.invocation_id for run in eligible}),
        len({run.session_id for run in eligible}),
        len({(run.started_at.to_iso(), run.ended_at.to_iso()) for run in eligible}),
    )
    if len(fingerprints) != 1:
        return min(independent, count), "run fingerprint drift"
    if independent != len(eligible):
        return independent, "independent invocation/session evidence is required"
    return independent, None


def assess_verified_runs(
    *,
    verified_runs: tuple[VerifiedValidationRun, ...],
    evidence_relative_path: str,
    evidence_sha256: str,
    created_at: UtcTimestamp,
    sample_eligibility: SampleEligibility,
) -> SourceCandidateAssessment:
    """Derive an assessment without accepting caller-supplied gates or identity."""
    if not verified_runs:
        raise ValueError("at least one verified run is required")
    first = verified_runs[0]
    if any(
        run.assessment_id != first.assessment_id
        or run.source != first.source
        or run.dataset != first.dataset
        for run in verified_runs
    ):
        raise ValueError("verified runs must bind one source x dataset assessment")
    gates = aggregate_gates(verified_runs)
    stability_runs, independence_reason = _stable_count(verified_runs)
    all_real = all(run.evidence_kind is EvidenceKind.REAL for run in verified_runs)
    all_successful = all(run.failure is None and run.gates.passed for run in verified_runs)
    same_versions = all(run.versions == first.versions for run in verified_runs)
    eligible_sample = sample_eligibility is SampleEligibility.PRODUCTION_ELIGIBLE
    ready = (
        all_real
        and all_successful
        and same_versions
        and eligible_sample
        and independence_reason is None
        and stability_runs >= MIN_PRODUCTION_STABILITY_RUNS
    )
    reasons: list[str] = []
    if not all_real:
        reasons.append("verifier-trusted REAL runner evidence is required")
    for field, passed in gates.to_dict().items():
        if not passed:
            reasons.append(f"{field} is false")
    if not same_versions or independence_reason == "run fingerprint drift":
        reasons.append("run fingerprint drift")
    elif independence_reason:
        reasons.append(independence_reason)
    if not eligible_sample:
        reasons.append("controlled fixed sample is FRAMEWORK_ONLY")
    if stability_runs < MIN_PRODUCTION_STABILITY_RUNS:
        reasons.append("three independent stable runs are required")
    return SourceCandidateAssessment(
        assessment_id=first.assessment_id,
        source=first.source,
        dataset=first.dataset,
        evidence_kind=EvidenceKind.REAL if all_real else EvidenceKind.SYNTHETIC,
        semantic_contract_verified=gates.semantic_contract_verified,
        schema_unit_verified=gates.schema_unit_verified,
        coverage_adequate=gates.coverage_adequate,
        quality_passed=gates.quality_passed,
        fixed_sample_differences_adequate=gates.fixed_sample_differences_adequate,
        stability_runs=stability_runs,
        status=(
            AssessmentStatus.PRODUCTION_READY
            if ready
            else AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION
        ),
        blocking_reason=None if ready else "; ".join(dict.fromkeys(reasons)),
        assessment_config_version=first.versions.config_version,
        sample_spec_version=first.versions.sample_spec_version,
        rule_version=first.versions.rule_version,
        evidence_relative_path=evidence_relative_path,
        evidence_sha256=evidence_sha256,
        created_at=created_at,
    )


def common_fingerprint(runs: tuple[VerifiedValidationRun, ...]) -> str | None:
    values = {run.run_fingerprint for run in runs}
    return next(iter(values)) if len(values) == 1 else None


__all__ = ["aggregate_gates", "assess_verified_runs", "common_fingerprint"]
