"""Trust-separated contracts for source-validation evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import PurePosixPath

from quantking.common.ids import EntityId
from quantking.common.time import UtcTimestamp
from quantking.data.assessment.models import (
    AssessmentStatus,
    EvidenceKind,
    SourceCandidateAssessment,
)
from quantking.data.models import DatasetKind
from quantking.data.validation.samples import SampleEligibility

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
_RELATIVE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./=-]{0,255}$")
_SAFE_FAILURES = frozenset(
    {
        "AUTHORIZATION_REQUIRED",
        "CAPABILITY_UNSUPPORTED",
        "CONTRACT_FAILURE",
        "EXTERNAL_UNAVAILABLE",
        "MAINTENANCE",
        "PERMISSION_DENIED",
        "RATE_LIMITED",
        "RUNNER_UNAVAILABLE",
        "SDK_LOAD_FAILURE",
        "VERIFICATION_FAILED",
    }
)
_VERIFICATION_SEAL = object()


def _sha(value: str, field: str) -> None:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256")


def _entity(value: EntityId, namespace: str, field: str) -> None:
    if not isinstance(value, EntityId) or value.namespace != namespace:
        raise ValueError(f"{field} must use {namespace} namespace")


@dataclass(frozen=True, slots=True)
class ValidationVersions:
    adapter_version: str
    package_version: str
    code_version: str
    schema_version: str
    rule_version: str
    threshold_version: str
    sample_spec_version: str
    config_version: str

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            if not isinstance(value, str) or not _VERSION.fullmatch(value):
                raise ValueError(f"{field_name} must be a stable non-sensitive version")

    def to_dict(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class AssessmentGates:
    semantic_contract_verified: bool
    schema_unit_verified: bool
    coverage_adequate: bool
    quality_passed: bool
    fixed_sample_differences_adequate: bool

    def __post_init__(self) -> None:
        if any(type(getattr(self, field)) is not bool for field in self.__dataclass_fields__):
            raise TypeError("assessment gates must be bool values")

    @classmethod
    def none_passed(cls) -> AssessmentGates:
        return cls(False, False, False, False, False)

    @classmethod
    def all_passed(cls) -> AssessmentGates:
        """Test convenience only; writers and assessors never accept this value."""
        return cls(True, True, True, True, True)

    @property
    def passed(self) -> bool:
        return all(getattr(self, field) for field in self.__dataclass_fields__)

    def to_dict(self) -> dict[str, bool]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    category: str
    retryable: bool

    def __post_init__(self) -> None:
        if self.category not in _SAFE_FAILURES:
            raise ValueError("failure category must be stable and sanitized")
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be bool")


@dataclass(frozen=True, slots=True)
class ValidationRunEvidence:
    """Unverified writer envelope.

    It deliberately contains no evidence kind, gates, content hashes or quality
    verdicts. Those values only exist on :class:`VerifiedValidationRun`.
    """

    run_id: EntityId
    assessment_id: EntityId
    invocation_id: EntityId
    authorization_id: EntityId
    source: str
    dataset: DatasetKind
    versions: ValidationVersions
    request_sha256: str
    sample_spec_sha256: str
    run_fingerprint: str
    started_at: UtcTimestamp
    ended_at: UtcTimestamp
    difference_report_id: EntityId | None = None
    failure: ValidationFailure | None = None

    def __post_init__(self) -> None:
        _entity(self.run_id, "data_validation", "run_id")
        _entity(self.assessment_id, "data_assessment", "assessment_id")
        _entity(self.invocation_id, "data_invocation", "invocation_id")
        _entity(self.authorization_id, "data_authorization", "authorization_id")
        if not isinstance(self.source, str) or not _SOURCE.fullmatch(self.source):
            raise ValueError("source must be a stable uppercase identifier")
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("dataset must be a DatasetKind")
        for field in ("request_sha256", "sample_spec_sha256", "run_fingerprint"):
            _sha(getattr(self, field), field)
        if self.ended_at.value <= self.started_at.value:
            raise ValueError("ended_at must be later than started_at")
        if self.difference_report_id is not None:
            _entity(self.difference_report_id, "data_diff", "difference_report_id")


@dataclass(frozen=True, slots=True)
class VerifiedValidationRun:
    """A verifier-derived run. Construction requires the module-private issuer."""

    run_id: EntityId
    assessment_id: EntityId
    invocation_id: EntityId
    session_id: EntityId
    authorization_id: EntityId
    source: str
    dataset: DatasetKind
    versions: ValidationVersions
    request_sha256: str
    sample_spec_sha256: str
    run_fingerprint: str
    evidence_kind: EvidenceKind
    gates: AssessmentGates
    raw_manifest_relative_path: str | None
    raw_manifest_sha256: str | None
    canonical_content_sha256: str | None
    quality_run_id: EntityId | None
    quality_manifest_sha256: str | None
    difference_report_id: EntityId | None
    difference_report_sha256: str | None
    sample_eligibility: SampleEligibility
    started_at: UtcTimestamp
    ended_at: UtcTimestamp
    failure: ValidationFailure | None
    verification_reason: str | None
    _seal: object = dataclass_field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _VERIFICATION_SEAL:
            raise ValueError("verified runs can only be issued by the deep verifier")
        _entity(self.run_id, "data_validation", "run_id")
        _entity(self.assessment_id, "data_assessment", "assessment_id")
        _entity(self.invocation_id, "data_invocation", "invocation_id")
        _entity(self.session_id, "data_session", "session_id")
        _entity(self.authorization_id, "data_authorization", "authorization_id")
        for field in ("request_sha256", "sample_spec_sha256", "run_fingerprint"):
            _sha(getattr(self, field), field)
        if self.evidence_kind is EvidenceKind.REAL and self.failure is not None:
            raise ValueError("failed verification cannot establish REAL identity")
        if self.gates.fixed_sample_differences_adequate and (
            self.difference_report_id is None or self.difference_report_sha256 is None
        ):
            raise ValueError("difference gate requires a verified DifferenceReport")
        if (
            self.sample_eligibility is not SampleEligibility.PRODUCTION_ELIGIBLE
            and self.gates.fixed_sample_differences_adequate
        ):
            raise ValueError("incomplete sample cannot pass fixed-sample gate")

    @property
    def status(self) -> AssessmentStatus:
        return AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION


@dataclass(frozen=True, slots=True)
class RealSourceValidationRun(VerifiedValidationRun):
    def __post_init__(self) -> None:
        super(RealSourceValidationRun, self).__post_init__()
        if self.evidence_kind is not EvidenceKind.REAL:
            raise ValueError("RealSourceValidationRun requires verifier-derived REAL identity")


@dataclass(frozen=True, slots=True)
class VerifiedAssessmentEvidence:
    assessment: SourceCandidateAssessment
    verified_runs: tuple[VerifiedValidationRun, ...]
    evidence_manifest_sha256: str
    evidence_relative_path: str
    common_run_fingerprint: str | None
    sample_eligibility: SampleEligibility
    _seal: object = dataclass_field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _VERIFICATION_SEAL:
            raise ValueError("VerifiedAssessmentEvidence can only be issued by a verifier")
        _sha(self.evidence_manifest_sha256, "evidence_manifest_sha256")
        relative = PurePosixPath(self.evidence_relative_path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not _RELATIVE.fullmatch(self.evidence_relative_path)
        ):
            raise ValueError("evidence_relative_path must be a managed relative path")
        if not self.verified_runs:
            raise ValueError("verified assessment evidence requires at least one run")
        if any(
            run.assessment_id != self.assessment.assessment_id
            or run.source != self.assessment.source
            or run.dataset != self.assessment.dataset
            for run in self.verified_runs
        ):
            raise ValueError("verified runs must bind one assessment cell")
        if self.assessment.evidence_relative_path != self.evidence_relative_path:
            raise ValueError("assessment evidence path must bind the verified manifest")
        if self.assessment.evidence_sha256 != self.evidence_manifest_sha256:
            raise ValueError("assessment evidence SHA must bind the verified manifest")
        if self.assessment.status is AssessmentStatus.PRODUCTION_READY and (
            self.sample_eligibility is not SampleEligibility.PRODUCTION_ELIGIBLE
            or self.common_run_fingerprint is None
            or any(run.run_fingerprint != self.common_run_fingerprint for run in self.verified_runs)
        ):
            raise ValueError("production assessment lacks frozen eligible evidence")


__all__ = [
    "AssessmentGates",
    "RealSourceValidationRun",
    "ValidationFailure",
    "ValidationRunEvidence",
    "ValidationVersions",
    "VerifiedAssessmentEvidence",
    "VerifiedValidationRun",
]
