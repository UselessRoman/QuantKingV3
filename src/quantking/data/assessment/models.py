"""Framework-free source-candidate assessment domain objects (S1-04B).

A :class:`SourceCandidateAssessment` records the evidence collected for one
``source x dataset`` pair without allowing synthetic data to certify
production readiness. The model is deliberately independent of SQLAlchemy,
DuckDB, PyArrow, vendor SDKs and FastAPI; persistent ORM rows live in
``data.adapters.persistence`` and are mapped to and from these objects at
the adapter boundary.

Business rules enforced here:

* ``EvidenceKind.SYNTHETIC`` cannot carry ``AssessmentStatus.PRODUCTION_READY``.
* ``AssessmentStatus.UNVERIFIED`` and ``AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION``
  require a non-empty ``blocking_reason``.
* ``AssessmentStatus.PRODUCTION_READY`` cannot carry a ``blocking_reason``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from quantking.common.ids import EntityId
from quantking.common.time import UtcTimestamp
from quantking.data.models import DatasetKind

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MANAGED_RELATIVE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./=-]*\Z")


def _text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value


def _sha256(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"{field_name} must be a 64-character lowercase hex SHA-256")
    return value


def _managed_relative(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or len(candidate.drive) != 0:
        raise ValueError(f"{field_name} must be a relative path, not absolute")
    if ".." in candidate.parts:
        raise ValueError(f"{field_name} must not contain parent traversal")
    if not _MANAGED_RELATIVE.fullmatch(value):
        raise ValueError(f"{field_name} must be a managed relative identifier")
    return value


class EvidenceKind(StrEnum):
    """Kind of evidence backing a candidate assessment.

    ``SYNTHETIC`` evidence is produced from deterministic, in-process fixtures
    and can never certify production readiness. ``REAL`` evidence is produced
    from a live external supplier response under explicit authorization.
    """

    SYNTHETIC = "SYNTHETIC"
    REAL = "REAL"


class AssessmentStatus(StrEnum):
    """Lifecycle of one source-candidate assessment.

    ``UNVERIFIED`` means no evidence has been collected yet.
    ``BLOCKED_EXTERNAL_VALIDATION`` means synthetic evidence exists but a
    real external validation run is still required before production use.
    ``PRODUCTION_READY`` means real evidence has passed every gate and the
    source is eligible for explicit override use.
    """

    UNVERIFIED = "UNVERIFIED"
    BLOCKED_EXTERNAL_VALIDATION = "BLOCKED_EXTERNAL_VALIDATION"
    PRODUCTION_READY = "PRODUCTION_READY"


class SourceRole(StrEnum):
    """Role a source plays in the production matrix.

    ``PRIMARY`` is the designated production source (XTQuant).
    ``FALLBACK`` is a candidate source that may be explicitly chosen via a
    :class:`SourceOverride` when the primary is unavailable.
    """

    PRIMARY = "PRIMARY"
    FALLBACK = "FALLBACK"


_BLOCKED_STATUSES = frozenset(
    {AssessmentStatus.UNVERIFIED, AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION}
)
MIN_PRODUCTION_STABILITY_RUNS = 3


@dataclass(frozen=True, slots=True)
class SourceCandidateAssessment:
    """One source x dataset candidate assessment record.

    The assessment binds to a managed, immutable evidence artifact (relative
    path + SHA-256) and carries explicit ``assessment_config_version``,
    ``sample_spec_version`` and ``rule_version`` so the evidence lineage is
    reproducible. Synthetic evidence can never certify production readiness;
    blocked statuses must declare a ``blocking_reason``.
    """

    assessment_id: EntityId
    source: str
    dataset: DatasetKind
    evidence_kind: EvidenceKind
    semantic_contract_verified: bool
    schema_unit_verified: bool
    coverage_adequate: bool
    quality_passed: bool
    fixed_sample_differences_adequate: bool
    stability_runs: int
    status: AssessmentStatus
    blocking_reason: str | None
    assessment_config_version: str
    sample_spec_version: str
    rule_version: str
    evidence_relative_path: str
    evidence_sha256: str
    created_at: UtcTimestamp

    def __post_init__(self) -> None:
        if (
            not isinstance(self.assessment_id, EntityId)
            or self.assessment_id.namespace != "data_assessment"
        ):
            raise ValueError("assessment_id must use the data_assessment namespace")
        _text(self.source, field_name="source")
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("dataset must be a DatasetKind")
        if not isinstance(self.evidence_kind, EvidenceKind):
            raise TypeError("evidence_kind must be an EvidenceKind")
        for field_name in (
            "semantic_contract_verified",
            "schema_unit_verified",
            "coverage_adequate",
            "quality_passed",
            "fixed_sample_differences_adequate",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be a bool")
        if (
            not isinstance(self.stability_runs, int)
            or isinstance(self.stability_runs, bool)
            or self.stability_runs < 0
        ):
            raise ValueError("stability_runs must be a non-negative integer")
        if not isinstance(self.status, AssessmentStatus):
            raise TypeError("status must be an AssessmentStatus")
        if self.blocking_reason is not None:
            _text(self.blocking_reason, field_name="blocking_reason")
        for field_name in (
            "assessment_config_version",
            "sample_spec_version",
            "rule_version",
        ):
            _text(getattr(self, field_name), field_name=field_name)
        object.__setattr__(
            self,
            "evidence_relative_path",
            _managed_relative(self.evidence_relative_path, field_name="evidence_relative_path"),
        )
        object.__setattr__(
            self,
            "evidence_sha256",
            _sha256(self.evidence_sha256, field_name="evidence_sha256"),
        )
        if not isinstance(self.created_at, UtcTimestamp):
            raise TypeError("created_at must be a UtcTimestamp")

        if (
            self.evidence_kind is EvidenceKind.SYNTHETIC
            and self.status is AssessmentStatus.PRODUCTION_READY
        ):
            raise ValueError("synthetic evidence cannot certify production readiness")
        if self.status in _BLOCKED_STATUSES and not self.blocking_reason:
            raise ValueError(f"{self.status.value} status requires a blocking reason")
        if self.status is AssessmentStatus.PRODUCTION_READY and self.blocking_reason is not None:
            raise ValueError("PRODUCTION_READY status cannot carry a blocking reason")
        if self.status is AssessmentStatus.PRODUCTION_READY and (
            self.evidence_kind is not EvidenceKind.REAL
            or not all(
                (
                    self.semantic_contract_verified,
                    self.schema_unit_verified,
                    self.coverage_adequate,
                    self.quality_passed,
                    self.fixed_sample_differences_adequate,
                )
            )
            or self.stability_runs < MIN_PRODUCTION_STABILITY_RUNS
        ):
            raise ValueError(
                "production readiness requires REAL evidence, every gate, and three stability runs"
            )


__all__ = [
    "MIN_PRODUCTION_STABILITY_RUNS",
    "AssessmentStatus",
    "EvidenceKind",
    "SourceCandidateAssessment",
    "SourceRole",
]
