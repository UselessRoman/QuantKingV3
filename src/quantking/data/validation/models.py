"""Strongly typed validation-run and verified-assessment evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import PurePosixPath

from quantking.common.ids import EntityId
from quantking.common.time import UtcTimestamp
from quantking.data.assessment.models import EvidenceKind, SourceCandidateAssessment
from quantking.data.models import DatasetKind

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
        "SDK_LOAD_FAILURE",
    }
)
_VERIFICATION_SEAL = object()


def _text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")


def _sha(value: str, field: str) -> None:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256")


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
    def all_passed(cls) -> AssessmentGates:
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
    run_id: EntityId
    assessment_id: EntityId
    source: str
    dataset: DatasetKind
    evidence_kind: EvidenceKind
    versions: ValidationVersions
    request_sha256: str
    sample_spec_sha256: str
    raw_manifest_relative_path: str
    raw_manifest_sha256: str
    canonical_content_sha256: str
    quality_run_id: EntityId
    quality_manifest_sha256: str
    difference_report_id: EntityId | None
    difference_report_sha256: str | None
    gates: AssessmentGates
    started_at: UtcTimestamp
    ended_at: UtcTimestamp
    failure: ValidationFailure | None

    def __post_init__(self) -> None:
        for field, namespace in (
            ("run_id", "data_validation"),
            ("assessment_id", "data_assessment"),
            ("quality_run_id", "data_quality"),
        ):
            value = getattr(self, field)
            if not isinstance(value, EntityId) or value.namespace != namespace:
                raise ValueError(f"{field} must use {namespace} namespace")
        if not isinstance(self.source, str) or not _SOURCE.fullmatch(self.source):
            raise ValueError("source must be a stable uppercase identifier")
        for field in (
            "request_sha256",
            "sample_spec_sha256",
            "raw_manifest_sha256",
            "canonical_content_sha256",
            "quality_manifest_sha256",
        ):
            _sha(getattr(self, field), field)
        relative = PurePosixPath(self.raw_manifest_relative_path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not _RELATIVE.fullmatch(self.raw_manifest_relative_path)
        ):
            raise ValueError("raw_manifest_relative_path must be a managed relative path")
        if self.difference_report_id is None:
            if self.difference_report_sha256 is not None:
                raise ValueError("difference report SHA requires an ID")
        else:
            if self.difference_report_id.namespace != "data_diff":
                raise ValueError("difference_report_id must use data_diff namespace")
            _sha(self.difference_report_sha256 or "", "difference_report_sha256")
        if self.ended_at.value < self.started_at.value:
            raise ValueError("ended_at must not precede started_at")
        if self.failure is not None and self.gates.passed:
            raise ValueError("failed validation cannot pass every gate")


@dataclass(frozen=True, slots=True)
class RealSourceValidationRun(ValidationRunEvidence):
    def __post_init__(self) -> None:
        super(RealSourceValidationRun, self).__post_init__()
        if self.evidence_kind is not EvidenceKind.REAL:
            raise ValueError("RealSourceValidationRun requires EvidenceKind.REAL")


@dataclass(frozen=True, slots=True)
class VerifiedAssessmentEvidence:
    assessment: SourceCandidateAssessment
    verified_runs: tuple[ValidationRunEvidence, ...]
    evidence_manifest_sha256: str
    evidence_relative_path: str
    _seal: object = dataclass_field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _VERIFICATION_SEAL:
            raise ValueError("VerifiedAssessmentEvidence can only be issued by a verifier")
        _sha(self.evidence_manifest_sha256, "evidence_manifest_sha256")
        _text(self.evidence_relative_path, "evidence_relative_path")
        if not self.verified_runs:
            raise ValueError("verified assessment evidence requires at least one run")
        if any(run.assessment_id != self.assessment.assessment_id for run in self.verified_runs):
            raise ValueError("all runs must bind the assessment_id")
        if any(
            run.source != self.assessment.source or run.dataset != self.assessment.dataset
            for run in self.verified_runs
        ):
            raise ValueError("all runs must bind the assessment source and dataset")
        if self.assessment.evidence_kind is EvidenceKind.REAL and any(
            not isinstance(run, RealSourceValidationRun) for run in self.verified_runs
        ):
            raise ValueError("REAL assessment requires verified RealSourceValidationRun values")
        if (
            self.assessment.evidence_kind is EvidenceKind.REAL
            and self.assessment.stability_runs != len({run.run_id for run in self.verified_runs})
        ):
            raise ValueError("REAL assessment stability must equal distinct verified run IDs")


__all__ = [
    "AssessmentGates",
    "RealSourceValidationRun",
    "ValidationFailure",
    "ValidationRunEvidence",
    "ValidationVersions",
    "VerifiedAssessmentEvidence",
]
