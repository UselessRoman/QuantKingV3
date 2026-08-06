"""Versioned S1-07C evidence contracts.

This module deliberately does not alter the accepted S1-07A v2 request,
attestation, or envelope semantics.  C0A evidence is an opt-in v3 family:
the request itself freezes the full attempt identity, selected case set,
reference-bundle binding and the controlled source/dataset contracts.

There is intentionally no REAL signer implementation here.  A future C0B
runtime may provide a trusted signer through the narrow protocol below; the
only bundled signer is permanently SYNTHETIC.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from quantking.common.ids import EntityId, parse_entity_id
from quantking.common.time import UtcTimestamp
from quantking.data.assessment.models import EvidenceKind
from quantking.data.models import DatasetKind
from quantking.data.validation.models import ValidationFailure, ValidationVersions
from quantking.data.validation.samples import FixedSampleSpec

REQUEST_SCHEMA_V3 = "source-validation-request.v2"
RUN_SCHEMA_V3 = "source-validation-envelope.v3"
RESULT_ATTESTATION_SCHEMA_V3 = "source-validation-result-attestation.v2"
ASSESSMENT_SCHEMA_V3 = "source-assessment-manifest.v3"

_CASE_ID = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
_BUNDLE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
_VERSION_FIELDS = frozenset(
    {
        "adapter_version",
        "package_version",
        "code_version",
        "schema_version",
        "rule_version",
        "threshold_version",
        "sample_spec_version",
        "config_version",
    }
)


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _sha(value: str, field: str) -> None:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256")


def _stable(value: str, field: str) -> None:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ValueError(f"{field} must be a stable version")


def _case_ids(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not values:
        raise ValueError(f"{field} must be a non-empty tuple")
    if any(not isinstance(item, str) or not _CASE_ID.fullmatch(item) for item in values):
        raise ValueError(f"{field} contains an invalid case ID")
    if tuple(sorted(values)) != values or len(set(values)) != len(values):
        raise ValueError(f"{field} must be sorted and unique")
    return values


def _identity(value: EntityId, namespace: str, field: str) -> None:
    if not isinstance(value, EntityId) or value.namespace != namespace:
        raise ValueError(f"{field} must use {namespace} namespace")


@dataclass(frozen=True, slots=True)
class ValidationAttemptIdentityV3:
    """Every identity a v3 request signs before an attempt is started."""

    assessment_id: EntityId
    authorization_id: EntityId
    invocation_id: EntityId
    session_id: EntityId
    run_id: EntityId

    def __post_init__(self) -> None:
        _identity(self.assessment_id, "data_assessment", "assessment_id")
        _identity(self.authorization_id, "data_authorization", "authorization_id")
        _identity(self.invocation_id, "data_invocation", "invocation_id")
        _identity(self.session_id, "data_session", "session_id")
        _identity(self.run_id, "data_validation", "run_id")

    def to_dict(self) -> dict[str, str]:
        return {
            "assessment_id": str(self.assessment_id),
            "authorization_id": str(self.authorization_id),
            "invocation_id": str(self.invocation_id),
            "run_id": str(self.run_id),
            "session_id": str(self.session_id),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ValidationAttemptIdentityV3:
        if set(raw) != {
            "assessment_id",
            "authorization_id",
            "invocation_id",
            "run_id",
            "session_id",
        }:
            raise ValueError("attempt identity fields do not match the schema")
        return cls(
            assessment_id=parse_entity_id(str(raw["assessment_id"])),
            authorization_id=parse_entity_id(str(raw["authorization_id"])),
            invocation_id=parse_entity_id(str(raw["invocation_id"])),
            session_id=parse_entity_id(str(raw["session_id"])),
            run_id=parse_entity_id(str(raw["run_id"])),
        )


@dataclass(frozen=True, slots=True)
class SourceDatasetContractV3:
    """Controlled semantic/unit/coverage contract for one source x dataset."""

    source: str
    dataset: DatasetKind
    contract_version: str
    semantic_rule_version: str
    unit_rule_version: str
    coverage_rule_version: str
    expected_schema_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not _SOURCE.fullmatch(self.source):
            raise ValueError("contract source must be a stable uppercase identifier")
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("contract dataset must be a DatasetKind")
        for field in (
            "contract_version",
            "semantic_rule_version",
            "unit_rule_version",
            "coverage_rule_version",
            "expected_schema_version",
        ):
            _stable(getattr(self, field), field)

    def to_dict(self) -> dict[str, str]:
        return {
            "contract_version": self.contract_version,
            "coverage_rule_version": self.coverage_rule_version,
            "dataset": self.dataset.value,
            "expected_schema_version": self.expected_schema_version,
            "semantic_rule_version": self.semantic_rule_version,
            "source": self.source,
            "unit_rule_version": self.unit_rule_version,
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class PromotionProfileV3:
    """Dataset-wide promotion requirements, independent of case execution approval."""

    source: str
    dataset: DatasetKind
    promotion_profile_version: str
    promotion_config_version: str
    required_case_ids: tuple[str, ...]
    reference_bundle_id: str
    reference_bundle_manifest_sha256: str
    minimum_stability_runs: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not _SOURCE.fullmatch(self.source):
            raise ValueError("promotion profile source must be a stable uppercase identifier")
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("promotion profile dataset must be a DatasetKind")
        _stable(self.promotion_profile_version, "promotion_profile_version")
        _stable(self.promotion_config_version, "promotion_config_version")
        _case_ids(self.required_case_ids, "required_case_ids")
        if not isinstance(self.reference_bundle_id, str) or not _BUNDLE.fullmatch(
            self.reference_bundle_id
        ):
            raise ValueError("reference_bundle_id is invalid")
        _sha(self.reference_bundle_manifest_sha256, "reference_bundle_manifest_sha256")
        if (
            not isinstance(self.minimum_stability_runs, int)
            or isinstance(self.minimum_stability_runs, bool)
            or self.minimum_stability_runs < 1
        ):
            raise ValueError("minimum_stability_runs must be a positive integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset.value,
            "minimum_stability_runs": self.minimum_stability_runs,
            "promotion_config_version": self.promotion_config_version,
            "promotion_profile_version": self.promotion_profile_version,
            "reference_bundle_id": self.reference_bundle_id,
            "reference_bundle_manifest_sha256": self.reference_bundle_manifest_sha256,
            "required_case_ids": list(self.required_case_ids),
            "source": self.source,
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class ValidationRequestManifestV3:
    """Canonical, identity-bound request for a selected v4/v5 case set."""

    identity: ValidationAttemptIdentityV3
    source: str
    dataset: DatasetKind
    sample_spec_version: str
    sample_spec_sha256: str
    selected_case_ids: tuple[str, ...]
    selected_cases: tuple[dict[str, object], ...]
    source_dataset_contract: SourceDatasetContractV3
    promotion_profile: PromotionProfileV3

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ValidationAttemptIdentityV3):
            raise TypeError("request identity must be a ValidationAttemptIdentityV3")
        if not isinstance(self.source, str) or not _SOURCE.fullmatch(self.source):
            raise ValueError("request source must be a stable uppercase identifier")
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("request dataset must be a DatasetKind")
        _stable(self.sample_spec_version, "sample_spec_version")
        _sha(self.sample_spec_sha256, "sample_spec_sha256")
        _case_ids(self.selected_case_ids, "selected_case_ids")
        if self.source_dataset_contract.source != self.source or (
            self.source_dataset_contract.dataset is not self.dataset
        ):
            raise ValueError("source/dataset contract does not match request")
        if self.promotion_profile.source != self.source or (
            self.promotion_profile.dataset is not self.dataset
        ):
            raise ValueError("promotion profile does not match request")
        if self.promotion_profile.promotion_config_version == "":  # defensive, unreachable
            raise ValueError("promotion profile config version is required")
        if not isinstance(self.selected_cases, tuple) or len(self.selected_cases) != len(
            self.selected_case_ids
        ):
            raise ValueError("selected cases must exactly cover selected_case_ids")

    @classmethod
    def for_controlled_sample(
        cls,
        *,
        identity: ValidationAttemptIdentityV3,
        source: str,
        dataset: DatasetKind,
        sample: FixedSampleSpec,
        selected_case_ids: tuple[str, ...],
        source_dataset_contract: SourceDatasetContractV3,
        promotion_profile: PromotionProfileV3,
    ) -> ValidationRequestManifestV3:
        selected_ids = _case_ids(selected_case_ids, "selected_case_ids")
        by_id = {case.case_id: case for case in sample.cases}
        cases = []
        for case_id in selected_ids:
            case = by_id.get(case_id)
            if case is None or case.dataset is not dataset or source not in case.applicable_sources:
                raise ValueError("selected cases do not belong to controlled source x dataset")
            cases.append(case.to_dict())
        if not set(promotion_profile.required_case_ids).issubset(by_id):
            raise ValueError("promotion profile contains an unknown controlled case")
        return cls(
            identity=identity,
            source=source,
            dataset=dataset,
            sample_spec_version=sample.sample_spec_version,
            sample_spec_sha256=sample.sample_spec_sha256,
            selected_case_ids=selected_ids,
            selected_cases=tuple(cases),
            source_dataset_contract=source_dataset_contract,
            promotion_profile=promotion_profile,
        )

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "identity": self.identity.to_dict(),
            "promotion_profile": self.promotion_profile.to_dict(),
            "request_schema_version": REQUEST_SCHEMA_V3,
            "sample_spec_sha256": self.sample_spec_sha256,
            "sample_spec_version": self.sample_spec_version,
            "selected_case_ids": list(self.selected_case_ids),
            "selected_cases": list(self.selected_cases),
            "source": self.source,
            "source_dataset_contract": self.source_dataset_contract.to_dict(),
            "dataset": self.dataset.value,
        }

    @property
    def request_sha256(self) -> str:
        return sha256_json(self.unsigned_payload())

    @property
    def contract_sha256(self) -> str:
        """Stable selected-case contract digest, excluding per-attempt identities.

        ``request_sha256`` commits the authorization/invocation/session/run
        identities.  Stability comparisons need a separate digest because
        those identities must be fresh for every attempt; this digest freezes
        every non-identity input that must remain equal across the profile's
        independent runs.
        """
        payload = self.unsigned_payload()
        payload.pop("identity")
        return sha256_json(payload)

    def to_bytes(self) -> bytes:
        return canonical_json({**self.unsigned_payload(), "request_sha256": self.request_sha256})


def verify_request_manifest_v3(
    payload: bytes,
    *,
    source: str,
    dataset: DatasetKind,
    sample: FixedSampleSpec,
    source_dataset_contract: SourceDatasetContractV3,
    promotion_profile: PromotionProfileV3,
) -> ValidationRequestManifestV3:
    try:
        parsed: Any = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("v3 request manifest is invalid JSON") from error
    required = {
        "dataset",
        "identity",
        "promotion_profile",
        "request_schema_version",
        "request_sha256",
        "sample_spec_sha256",
        "sample_spec_version",
        "selected_case_ids",
        "selected_cases",
        "source",
        "source_dataset_contract",
    }
    if not isinstance(parsed, dict) or payload != canonical_json(parsed) or set(parsed) != required:
        raise ValueError("v3 request manifest fields do not match the schema")
    declared = parsed.pop("request_sha256")
    if parsed.get("request_schema_version") != REQUEST_SCHEMA_V3 or declared != sha256_json(parsed):
        raise ValueError("v3 request manifest schema or SHA mismatch")
    try:
        raw_identity = parsed["identity"]
        raw_ids = parsed["selected_case_ids"]
        if not isinstance(raw_identity, dict) or not isinstance(raw_ids, list):
            raise ValueError
        identity = ValidationAttemptIdentityV3.from_dict(raw_identity)
        request = ValidationRequestManifestV3.for_controlled_sample(
            identity=identity,
            source=source,
            dataset=dataset,
            sample=sample,
            selected_case_ids=tuple(str(item) for item in raw_ids),
            source_dataset_contract=source_dataset_contract,
            promotion_profile=promotion_profile,
        )
    except Exception as error:
        raise ValueError("v3 request manifest is invalid") from error
    if parsed != request.unsigned_payload():
        raise ValueError("v3 request does not exactly bind the controlled contract")
    return request


def compute_run_fingerprint_v3(
    *,
    request: ValidationRequestManifestV3,
    versions: ValidationVersions,
) -> str:
    if versions.sample_spec_version != request.sample_spec_version:
        raise ValueError("v3 versions do not bind the request sample version")
    if versions.schema_version != request.source_dataset_contract.expected_schema_version:
        raise ValueError("v3 schema version does not bind the source/dataset contract")
    if versions.config_version != request.promotion_profile.promotion_config_version:
        raise ValueError("v3 config version does not bind the promotion profile")
    return sha256_json(
        {
            "dataset": request.dataset.value,
            "promotion_profile_sha256": request.promotion_profile.sha256,
            "reference_bundle_id": request.promotion_profile.reference_bundle_id,
            "reference_bundle_manifest_sha256": (
                request.promotion_profile.reference_bundle_manifest_sha256
            ),
            "request_contract_sha256": request.contract_sha256,
            "sample_spec_sha256": request.sample_spec_sha256,
            "selected_case_ids": list(request.selected_case_ids),
            "source": request.source,
            "source_dataset_contract_sha256": request.source_dataset_contract.sha256,
            "versions": versions.to_dict(),
        }
    )


class AttemptOutcomeV3(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class SuccessEvidenceBindingV3:
    raw_manifest_sha256: str
    canonical_content_sha256: str
    quality_manifest_sha256: str
    quality_run_id: EntityId
    difference_manifest_sha256: str
    difference_report_id: EntityId
    semantic_evidence_sha256: str
    schema_unit_evidence_sha256: str
    evidence_inventory_sha256: str

    def __post_init__(self) -> None:
        for field in (
            "raw_manifest_sha256",
            "canonical_content_sha256",
            "quality_manifest_sha256",
            "difference_manifest_sha256",
            "semantic_evidence_sha256",
            "schema_unit_evidence_sha256",
            "evidence_inventory_sha256",
        ):
            _sha(getattr(self, field), field)
        _identity(self.quality_run_id, "data_quality", "quality_run_id")
        _identity(self.difference_report_id, "data_diff", "difference_report_id")

    def to_dict(self) -> dict[str, str]:
        return {
            "canonical_content_sha256": self.canonical_content_sha256,
            "difference_manifest_sha256": self.difference_manifest_sha256,
            "difference_report_id": str(self.difference_report_id),
            "evidence_inventory_sha256": self.evidence_inventory_sha256,
            "quality_manifest_sha256": self.quality_manifest_sha256,
            "quality_run_id": str(self.quality_run_id),
            "raw_manifest_sha256": self.raw_manifest_sha256,
            "schema_unit_evidence_sha256": self.schema_unit_evidence_sha256,
            "semantic_evidence_sha256": self.semantic_evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class ResultAttestationV3:
    """Signed v3 attempt outcome; it contains no caller-provided gate booleans."""

    signer_key_id: str
    identity: ValidationAttemptIdentityV3
    source: str
    dataset: DatasetKind
    request_sha256: str
    run_fingerprint: str
    selected_case_ids: tuple[str, ...]
    sample_spec_version: str
    sample_spec_sha256: str
    source_dataset_contract_version: str
    source_dataset_contract_sha256: str
    promotion_profile_version: str
    promotion_profile_sha256: str
    reference_bundle_id: str
    reference_bundle_manifest_sha256: str
    versions: Mapping[str, str]
    outcome: AttemptOutcomeV3
    failure: ValidationFailure | None
    success_evidence: SuccessEvidenceBindingV3 | None
    started_at: UtcTimestamp
    ended_at: UtcTimestamp
    signature: str

    def __post_init__(self) -> None:
        if not isinstance(self.signer_key_id, str) or not self.signer_key_id:
            raise ValueError("signer_key_id must be non-empty")
        if not isinstance(self.identity, ValidationAttemptIdentityV3):
            raise TypeError("attestation identity must be a ValidationAttemptIdentityV3")
        if not isinstance(self.source, str) or not _SOURCE.fullmatch(self.source):
            raise ValueError("attestation source must be a stable uppercase identifier")
        if not isinstance(self.dataset, DatasetKind):
            raise TypeError("attestation dataset must be a DatasetKind")
        for field in (
            "request_sha256",
            "run_fingerprint",
            "sample_spec_sha256",
            "source_dataset_contract_sha256",
            "promotion_profile_sha256",
            "reference_bundle_manifest_sha256",
            "signature",
        ):
            _sha(getattr(self, field), field)
        for field in (
            "sample_spec_version",
            "source_dataset_contract_version",
            "promotion_profile_version",
        ):
            _stable(getattr(self, field), field)
        _case_ids(self.selected_case_ids, "selected_case_ids")
        if not _BUNDLE.fullmatch(self.reference_bundle_id):
            raise ValueError("reference_bundle_id is invalid")
        if not isinstance(self.versions, Mapping) or set(self.versions) != _VERSION_FIELDS:
            raise ValueError("v3 attestation versions are incomplete")
        ValidationVersions(**dict(self.versions))
        object.__setattr__(self, "versions", MappingProxyType(dict(sorted(self.versions.items()))))
        if self.ended_at.value <= self.started_at.value:
            raise ValueError("attestation end must follow start")
        if self.failure is not None and not isinstance(self.failure, ValidationFailure):
            raise TypeError("attestation failure must be a ValidationFailure")
        if self.success_evidence is not None and not isinstance(
            self.success_evidence, SuccessEvidenceBindingV3
        ):
            raise TypeError("attestation success_evidence must be a SuccessEvidenceBindingV3")
        if self.outcome is AttemptOutcomeV3.SUCCEEDED:
            if self.failure is not None or self.success_evidence is None:
                raise ValueError("a successful v3 attempt requires complete success evidence")
        elif self.outcome is AttemptOutcomeV3.FAILED:
            if self.failure is None or self.success_evidence is not None:
                raise ValueError("a failed v3 attempt cannot carry success evidence")
        else:
            raise TypeError("outcome must be an AttemptOutcomeV3")

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "attempt_outcome": self.outcome.value,
            "dataset": self.dataset.value,
            "failure": (
                {"category": self.failure.category, "retryable": self.failure.retryable}
                if self.failure is not None
                else None
            ),
            "identity": self.identity.to_dict(),
            "promotion_profile_sha256": self.promotion_profile_sha256,
            "promotion_profile_version": self.promotion_profile_version,
            "reference_bundle_id": self.reference_bundle_id,
            "reference_bundle_manifest_sha256": self.reference_bundle_manifest_sha256,
            "request_sha256": self.request_sha256,
            "result_attestation_schema_version": RESULT_ATTESTATION_SCHEMA_V3,
            "run_fingerprint": self.run_fingerprint,
            "sample_spec_sha256": self.sample_spec_sha256,
            "sample_spec_version": self.sample_spec_version,
            "selected_case_ids": list(self.selected_case_ids),
            "signer_key_id": self.signer_key_id,
            "source": self.source,
            "source_dataset_contract_sha256": self.source_dataset_contract_sha256,
            "source_dataset_contract_version": self.source_dataset_contract_version,
            "started_at": self.started_at.to_iso(),
            "success_evidence": (
                self.success_evidence.to_dict() if self.success_evidence is not None else None
            ),
            "versions": dict(self.versions),
            "ended_at": self.ended_at.to_iso(),
        }

    def to_bytes(self) -> bytes:
        return canonical_json({**self.unsigned_payload(), "signature": self.signature})

    @classmethod
    def from_bytes(cls, payload: bytes) -> ResultAttestationV3:
        try:
            raw: Any = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("v3 result attestation is invalid JSON") from error
        expected = {
            "attempt_outcome",
            "dataset",
            "ended_at",
            "failure",
            "identity",
            "promotion_profile_sha256",
            "promotion_profile_version",
            "reference_bundle_id",
            "reference_bundle_manifest_sha256",
            "request_sha256",
            "result_attestation_schema_version",
            "run_fingerprint",
            "sample_spec_sha256",
            "sample_spec_version",
            "selected_case_ids",
            "signature",
            "signer_key_id",
            "source",
            "source_dataset_contract_sha256",
            "source_dataset_contract_version",
            "started_at",
            "success_evidence",
            "versions",
        }
        if (
            not isinstance(raw, dict)
            or payload != canonical_json(raw)
            or set(raw) != expected
            or raw.get("result_attestation_schema_version") != RESULT_ATTESTATION_SCHEMA_V3
        ):
            raise ValueError("v3 result attestation fields do not match the schema")
        try:
            raw_identity = raw["identity"]
            raw_failure = raw["failure"]
            raw_success = raw["success_evidence"]
            if not isinstance(raw_identity, dict) or not isinstance(raw["selected_case_ids"], list):
                raise ValueError
            failure = (
                None
                if raw_failure is None
                else ValidationFailure(
                    category=str(raw_failure["category"]),
                    retryable=raw_failure["retryable"],
                )
            )
            success = (
                None
                if raw_success is None
                else SuccessEvidenceBindingV3(
                    raw_manifest_sha256=str(raw_success["raw_manifest_sha256"]),
                    canonical_content_sha256=str(raw_success["canonical_content_sha256"]),
                    quality_manifest_sha256=str(raw_success["quality_manifest_sha256"]),
                    quality_run_id=parse_entity_id(str(raw_success["quality_run_id"])),
                    difference_manifest_sha256=str(raw_success["difference_manifest_sha256"]),
                    difference_report_id=parse_entity_id(str(raw_success["difference_report_id"])),
                    semantic_evidence_sha256=str(raw_success["semantic_evidence_sha256"]),
                    schema_unit_evidence_sha256=str(raw_success["schema_unit_evidence_sha256"]),
                    evidence_inventory_sha256=str(raw_success["evidence_inventory_sha256"]),
                )
            )
            return cls(
                signer_key_id=str(raw["signer_key_id"]),
                identity=ValidationAttemptIdentityV3.from_dict(raw_identity),
                source=str(raw["source"]),
                dataset=DatasetKind(str(raw["dataset"])),
                request_sha256=str(raw["request_sha256"]),
                run_fingerprint=str(raw["run_fingerprint"]),
                selected_case_ids=tuple(str(item) for item in raw["selected_case_ids"]),
                sample_spec_version=str(raw["sample_spec_version"]),
                sample_spec_sha256=str(raw["sample_spec_sha256"]),
                source_dataset_contract_version=str(raw["source_dataset_contract_version"]),
                source_dataset_contract_sha256=str(raw["source_dataset_contract_sha256"]),
                promotion_profile_version=str(raw["promotion_profile_version"]),
                promotion_profile_sha256=str(raw["promotion_profile_sha256"]),
                reference_bundle_id=str(raw["reference_bundle_id"]),
                reference_bundle_manifest_sha256=str(raw["reference_bundle_manifest_sha256"]),
                versions={str(key): str(value) for key, value in raw["versions"].items()},
                outcome=AttemptOutcomeV3(str(raw["attempt_outcome"])),
                failure=failure,
                success_evidence=success,
                started_at=UtcTimestamp.from_iso(str(raw["started_at"])),
                ended_at=UtcTimestamp.from_iso(str(raw["ended_at"])),
                signature=str(raw["signature"]),
            )
        except Exception as error:
            raise ValueError("v3 result attestation is invalid") from error


class V3ResultAttestationSigner(Protocol):
    @property
    def key_id(self) -> str: ...

    @property
    def evidence_kind(self) -> EvidenceKind: ...

    def verify(self, attestation: ResultAttestationV3) -> bool: ...


class FakeV3RunnerTrustStore:
    """The only bundled v3 signer; it is permanently SYNTHETIC."""

    KEY_ID = "quantking.fake-runner.v1"
    _KEY = b"quantking-s1-07a-r2-fake-runner-key"

    @property
    def key_id(self) -> str:
        return self.KEY_ID

    @property
    def evidence_kind(self) -> EvidenceKind:
        return EvidenceKind.SYNTHETIC

    def sign(self, attestation: ResultAttestationV3) -> ResultAttestationV3:
        if attestation.signer_key_id != self.key_id:
            raise ValueError("attestation signer_key_id does not match Fake signer")
        signature = hmac.new(
            self._KEY, canonical_json(attestation.unsigned_payload()), hashlib.sha256
        )
        return dataclass_replace(attestation, signature=signature.hexdigest())

    def verify(self, attestation: ResultAttestationV3) -> bool:
        expected = hmac.new(
            self._KEY, canonical_json(attestation.unsigned_payload()), hashlib.sha256
        )
        return attestation.signer_key_id == self.key_id and hmac.compare_digest(
            expected.hexdigest(), attestation.signature
        )


def dataclass_replace(value: ResultAttestationV3, **changes: object) -> ResultAttestationV3:
    """Avoid exposing an extra mutable signer DTO in the public contract."""
    values = {field: getattr(value, field) for field in value.__dataclass_fields__}
    values.update(changes)
    return ResultAttestationV3(**values)


__all__ = [
    "ASSESSMENT_SCHEMA_V3",
    "REQUEST_SCHEMA_V3",
    "RESULT_ATTESTATION_SCHEMA_V3",
    "RUN_SCHEMA_V3",
    "AttemptOutcomeV3",
    "FakeV3RunnerTrustStore",
    "PromotionProfileV3",
    "ResultAttestationV3",
    "SourceDatasetContractV3",
    "SuccessEvidenceBindingV3",
    "V3ResultAttestationSigner",
    "ValidationAttemptIdentityV3",
    "ValidationRequestManifestV3",
    "canonical_json",
    "compute_run_fingerprint_v3",
    "sha256_json",
    "verify_request_manifest_v3",
]
