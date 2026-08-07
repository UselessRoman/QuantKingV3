"""R2-specific immutable REAL evidence and lineage verifier.

The C0B v2/v3 artifact contracts remain byte-for-byte untouched.  REAL R2
uses this explicit family because every run must carry an authorization-file,
plan, and immutable claim-receipt lineage into the sealed read path.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from quantking.common.paths import is_reparse_point
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.warehouse.manifest import compute_sha256, path_safe_id
from quantking.data.assessment.models import (
    AssessmentStatus,
    EvidenceKind,
    SourceCandidateAssessment,
)
from quantking.data.models import DatasetKind
from quantking.data.validation.artifacts import (
    ValidationEvidenceConflictError,
    ValidationEvidenceVerificationError,
)
from quantking.data.validation.artifacts_v3 import V3EvidenceTrustContext
from quantking.data.validation.contracts_v3 import AttemptOutcomeV3, ValidationAttemptIdentityV3
from quantking.data.validation.models import ValidationFailure, ValidationVersions
from quantking.data.validation.real_authorization import (
    ExplicitLocalRealKey,
    RealClaimReceipt,
    RealClaimReceiptVerifier,
    RealRuntimeAuthorizationEnvelope,
    RealRuntimeAuthorizationVerifier,
    RealVerifierTrustAnchor,
)

REAL_RUN_SCHEMA = "source-validation-real-envelope.v2"
REAL_RESULT_ATTESTATION_SCHEMA = "source-validation-real-result-attestation.v2"
REAL_ASSESSMENT_SCHEMA = "source-assessment-real-manifest.v2"
_SOURCE = "XTQUANT"
_DATASET = DatasetKind.TRADING_STATUS
_CASE_IDS = ("st_warning_603377_sh",)
_FILES_FAILURE = frozenset(
    {
        "authorization/claim-receipt.json",
        "authorization/envelope.json",
        "failure/attempt.json",
        "request/plan.json",
        "runner/result-attestation.real.v2.json",
    }
)


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _sha(value: bytes) -> str:
    return compute_sha256(value)


def _is_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(item in "0123456789abcdef" for item in value)
    )


def _fixed_identity(identity: ValidationAttemptIdentityV3) -> None:
    if not isinstance(identity, ValidationAttemptIdentityV3):
        raise ValueError("REAL evidence identity is invalid")


@dataclass(frozen=True, slots=True)
class RealRunLineage:
    authorization_envelope_sha256: str
    plan_sha256: str
    claim_receipt_sha256: str

    def __post_init__(self) -> None:
        if not all(_is_sha(value) for value in self.to_dict().values()):
            raise ValueError("REAL evidence lineage digest is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "authorization_envelope_sha256": self.authorization_envelope_sha256,
            "claim_receipt_sha256": self.claim_receipt_sha256,
            "plan_sha256": self.plan_sha256,
        }


@dataclass(frozen=True, slots=True)
class RealResultAttestation:
    signer_key_id: str
    identity: ValidationAttemptIdentityV3
    source: str
    dataset: DatasetKind
    selected_case_ids: tuple[str, ...]
    versions: Mapping[str, str]
    outcome: AttemptOutcomeV3
    failure: ValidationFailure | None
    started_at: UtcTimestamp
    ended_at: UtcTimestamp
    lineage: RealRunLineage
    signature: str

    def __post_init__(self) -> None:
        _fixed_identity(self.identity)
        if (
            self.source != _SOURCE
            or self.dataset is not _DATASET
            or self.selected_case_ids != _CASE_IDS
            or not isinstance(self.signer_key_id, str)
            or not isinstance(self.versions, Mapping)
            or set(self.versions)
            != {
                "adapter_version",
                "package_version",
                "code_version",
                "schema_version",
                "rule_version",
                "threshold_version",
                "sample_spec_version",
                "config_version",
            }
            or any(type(value) is not str for value in self.versions.values())
            or self.ended_at.value <= self.started_at.value
            or not isinstance(self.lineage, RealRunLineage)
            or not isinstance(self.signature, str)
            or len(self.signature) != 128
        ):
            raise ValueError("REAL result attestation is invalid")
        if self.outcome is not AttemptOutcomeV3.FAILED or not isinstance(
            self.failure, ValidationFailure
        ):
            raise ValueError("C1A REAL result must be a capability-blocked failure")
        object.__setattr__(self, "versions", MappingProxyType(dict(sorted(self.versions.items()))))

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "attempt_outcome": self.outcome.value,
            "dataset": self.dataset.value,
            "ended_at": self.ended_at.to_iso(),
            "failure": (
                {"category": self.failure.category, "retryable": self.failure.retryable}
                if self.failure is not None
                else None
            ),
            "identity": self.identity.to_dict(),
            "lineage": self.lineage.to_dict(),
            "result_attestation_schema_version": REAL_RESULT_ATTESTATION_SCHEMA,
            "selected_case_ids": list(self.selected_case_ids),
            "signer_key_id": self.signer_key_id,
            "source": self.source,
            "started_at": self.started_at.to_iso(),
            "versions": dict(self.versions),
        }

    def to_bytes(self) -> bytes:
        return canonical_json({**self.unsigned_payload(), "signature": self.signature})

    @classmethod
    def from_bytes(cls, payload: bytes) -> RealResultAttestation:
        try:
            raw = json.loads(payload)
            expected = {
                "attempt_outcome",
                "dataset",
                "ended_at",
                "failure",
                "identity",
                "lineage",
                "result_attestation_schema_version",
                "selected_case_ids",
                "signature",
                "signer_key_id",
                "source",
                "started_at",
                "versions",
            }
            if (
                not isinstance(raw, dict)
                or set(raw) != expected
                or payload != canonical_json(raw)
                or raw["result_attestation_schema_version"] != REAL_RESULT_ATTESTATION_SCHEMA
                or not isinstance(raw["identity"], dict)
                or not isinstance(raw["lineage"], dict)
                or not isinstance(raw["selected_case_ids"], list)
                or not isinstance(raw["versions"], dict)
            ):
                raise ValueError
            failure_raw = raw["failure"]
            if failure_raw is not None and (
                not isinstance(failure_raw, dict) or set(failure_raw) != {"category", "retryable"}
            ):
                raise ValueError
            return cls(
                signer_key_id=str(raw["signer_key_id"]),
                identity=ValidationAttemptIdentityV3.from_dict(raw["identity"]),
                source=str(raw["source"]),
                dataset=DatasetKind(str(raw["dataset"])),
                selected_case_ids=tuple(str(value) for value in raw["selected_case_ids"]),
                versions={str(key): str(value) for key, value in raw["versions"].items()},
                outcome=AttemptOutcomeV3(str(raw["attempt_outcome"])),
                failure=(
                    None
                    if failure_raw is None
                    else ValidationFailure(
                        category=str(failure_raw["category"]), retryable=failure_raw["retryable"]
                    )
                ),
                started_at=UtcTimestamp.from_iso(str(raw["started_at"])),
                ended_at=UtcTimestamp.from_iso(str(raw["ended_at"])),
                lineage=RealRunLineage(
                    authorization_envelope_sha256=str(
                        raw["lineage"]["authorization_envelope_sha256"]
                    ),
                    plan_sha256=str(raw["lineage"]["plan_sha256"]),
                    claim_receipt_sha256=str(raw["lineage"]["claim_receipt_sha256"]),
                ),
                signature=str(raw["signature"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("REAL result attestation is invalid") from error


@dataclass(frozen=True, slots=True)
class RealRunInputs:
    identity: ValidationAttemptIdentityV3
    context: V3EvidenceTrustContext
    versions: ValidationVersions
    plan_sha256: str
    plan_bytes: bytes
    authorization_bytes: bytes
    authorization_envelope_sha256: str
    claim_receipt: RealClaimReceipt
    signer: ExplicitLocalRealKey
    valid_from: UtcTimestamp
    expires_at: UtcTimestamp

    def __post_init__(self) -> None:
        _fixed_identity(self.identity)
        if (
            not _is_sha(self.plan_sha256)
            or not _plan_sha_matches(self.plan_bytes, self.plan_sha256)
            or _sha(self.authorization_bytes) != self.authorization_envelope_sha256
            or self.claim_receipt.authorization_id != self.identity.authorization_id
            or self.claim_receipt.invocation_id != self.identity.invocation_id
            or self.claim_receipt.run_id != self.identity.run_id
            or self.claim_receipt.authorization_envelope_sha256
            != self.authorization_envelope_sha256
            or self.claim_receipt.plan_sha256 != self.plan_sha256
        ):
            raise ValueError("REAL run inputs are not lineage-bound")

    @property
    def lineage(self) -> RealRunLineage:
        return RealRunLineage(
            authorization_envelope_sha256=self.authorization_envelope_sha256,
            plan_sha256=self.plan_sha256,
            claim_receipt_sha256=self.claim_receipt.sha256,
        )


@dataclass(frozen=True, slots=True)
class VerifiedRealRun:
    identity: ValidationAttemptIdentityV3
    outcome: AttemptOutcomeV3
    failure: ValidationFailure | None
    started_at: UtcTimestamp
    ended_at: UtcTimestamp
    lineage: RealRunLineage
    manifest_sha256: str
    attestation_sha256: str


@dataclass(frozen=True, slots=True)
class VerifiedRealAssessment:
    assessment: SourceCandidateAssessment
    runs: tuple[VerifiedRealRun, ...]
    manifest_sha256: str
    evidence_relative_path: str

    @property
    def evidence_manifest_sha256(self) -> str:
        """Common sealed-reader spelling across explicit evidence families."""
        return self.manifest_sha256


class RealValidationEvidenceWriter:
    """Production writer: it obtains timestamps from the runner's injected clock."""

    _DOMAIN = "quantking.s1-07c1a-r2.real-result-attestation.v2"

    def __init__(self, *, validation_root: Path) -> None:
        self._root = _validate_root(validation_root)

    def write_failure(
        self,
        *,
        inputs: RealRunInputs,
        failure: ValidationFailure,
        started_at: UtcTimestamp,
        ended_at: UtcTimestamp,
    ) -> Path:
        return self._write(
            inputs=inputs,
            failure=failure,
            started_at=started_at,
            ended_at=ended_at,
        )

    def _write(
        self,
        *,
        inputs: RealRunInputs,
        failure: ValidationFailure,
        started_at: UtcTimestamp,
        ended_at: UtcTimestamp,
    ) -> Path:
        if not (
            inputs.valid_from.value
            <= inputs.claim_receipt.claimed_at.value
            <= started_at.value
            < ended_at.value
            < inputs.expires_at.value
        ):
            raise ValueError("REAL evidence timestamps are outside the authorization window")
        unsigned = RealResultAttestation(
            signer_key_id=inputs.signer.key_id,
            identity=inputs.identity,
            source=_SOURCE,
            dataset=_DATASET,
            selected_case_ids=_CASE_IDS,
            versions=inputs.versions.to_dict(),
            outcome=AttemptOutcomeV3.FAILED,
            failure=failure,
            started_at=started_at,
            ended_at=ended_at,
            lineage=inputs.lineage,
            signature="0" * 128,
        )
        attestation = RealResultAttestation(
            **{
                field: getattr(unsigned, field)
                for field in unsigned.__dataclass_fields__
                if field != "signature"
            },
            signature=inputs.signer.sign(domain=self._DOMAIN, payload=unsigned.unsigned_payload()),
        )
        files: dict[str, bytes] = {
            "authorization/envelope.json": inputs.authorization_bytes,
            "authorization/claim-receipt.json": inputs.claim_receipt.to_bytes(),
            "request/plan.json": inputs.plan_bytes,
            "runner/result-attestation.real.v2.json": attestation.to_bytes(),
        }
        files["failure/attempt.json"] = canonical_json(
            {"failure": {"category": failure.category, "retryable": failure.retryable}}
        )
        return self._write_files(inputs=inputs, attestation=attestation, files=files)

    def _write_files(
        self,
        *,
        inputs: RealRunInputs,
        attestation: RealResultAttestation,
        files: Mapping[str, bytes],
    ) -> Path:
        if attestation.outcome is not AttemptOutcomeV3.FAILED or set(files) != _FILES_FAILURE:
            raise ValueError("REAL evidence file shape is invalid")
        inventory = [
            {"relative_path": name, "sha256": _sha(payload), "size": len(payload)}
            for name, payload in sorted(files.items())
        ]
        unsigned = {
            "attempt_outcome": attestation.outcome.value,
            "dataset": _DATASET.value,
            "files": inventory,
            "identity": inputs.identity.to_dict(),
            "lineage": attestation.lineage.to_dict(),
            "manifest_schema_version": REAL_RUN_SCHEMA,
            "selected_case_ids": list(_CASE_IDS),
            "source": _SOURCE,
            "started_at": attestation.started_at.to_iso(),
            "ended_at": attestation.ended_at.to_iso(),
        }
        manifest_sha = _sha(canonical_json(unsigned))
        manifest = canonical_json({**unsigned, "manifest_sha256": manifest_sha})
        complete = canonical_json({"manifest_sha256": manifest_sha})
        directory = _run_directory(self._root, inputs.identity)
        if directory.exists():
            if (directory / "manifest.json").read_bytes() == manifest and (
                directory / "COMPLETE"
            ).read_bytes() == complete:
                return directory
            raise ValidationEvidenceConflictError(
                "REAL validation run ID already exists with different content"
            )
        incomplete = directory.with_name(directory.name + ".incomplete")
        incomplete.mkdir(parents=True, exist_ok=False)
        try:
            for relative, payload in files.items():
                target = incomplete / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            (incomplete / "manifest.json").write_bytes(manifest)
            (incomplete / "COMPLETE").write_bytes(complete)
            os.replace(incomplete, directory)
        except Exception:
            with suppress(OSError):
                (incomplete / "failure.json").write_bytes(
                    canonical_json({"category": "EVIDENCE_WRITE_FAILURE", "retryable": False})
                )
            raise
        return directory

    def finalize_assessment(
        self, *, assessment_directory: Path, verifier: RealValidationEvidenceVerifier
    ) -> VerifiedRealAssessment:
        runs = tuple(
            verifier.verify_run(directory=child)
            for child in sorted(assessment_directory.iterdir(), key=lambda item: item.name)
            if child.is_dir()
            and not child.name.startswith("_assessment")
            and not child.name.endswith(".incomplete")
        )
        if not runs:
            raise ValueError("REAL assessment requires one completed run")
        summary = _assessment_unsigned(runs=runs, context=verifier.context)
        manifest_sha = _sha(canonical_json(summary))
        manifest = canonical_json({**summary, "manifest_sha256": manifest_sha})
        complete = canonical_json({"manifest_sha256": manifest_sha})
        directory = assessment_directory / "_assessment-real-v2"
        if directory.exists():
            if (directory / "manifest.json").read_bytes() != manifest:
                raise ValidationEvidenceConflictError(
                    "REAL assessment is already finalized with different evidence"
                )
            return verifier.verify_assessment(assessment_directory=assessment_directory)
        incomplete = assessment_directory / "_assessment-real-v2.incomplete"
        incomplete.mkdir(parents=False, exist_ok=False)
        (incomplete / "manifest.json").write_bytes(manifest)
        (incomplete / "COMPLETE").write_bytes(complete)
        os.replace(incomplete, directory)
        return verifier.verify_assessment(assessment_directory=assessment_directory)


class RealValidationEvidenceVerifier:
    _DOMAIN = RealValidationEvidenceWriter._DOMAIN

    def __init__(
        self,
        *,
        validation_root: Path,
        context: V3EvidenceTrustContext,
        runner_verifier_anchor: RealVerifierTrustAnchor,
        authorization_verifier_anchor: RealVerifierTrustAnchor,
    ) -> None:
        self._root = _validate_root(validation_root)
        self.context = context
        self._runner_anchor = runner_verifier_anchor
        self._authorization_anchor = authorization_verifier_anchor
        self._runtime_authorization_verifier = RealRuntimeAuthorizationVerifier(
            trust_anchor=authorization_verifier_anchor
        )
        self._claim_receipt_verifier = RealClaimReceiptVerifier(
            trust_anchor=authorization_verifier_anchor
        )

    def verify_run(self, *, directory: Path) -> VerifiedRealRun:
        try:
            directory.relative_to(self._root)
            manifest = _load_canonical(directory / "manifest.json")
            complete = _load_canonical(directory / "COMPLETE")
            expected_manifest = {
                "attempt_outcome",
                "dataset",
                "ended_at",
                "files",
                "identity",
                "lineage",
                "manifest_schema_version",
                "manifest_sha256",
                "selected_case_ids",
                "source",
                "started_at",
            }
            if set(manifest) != expected_manifest or complete != {
                "manifest_sha256": manifest["manifest_sha256"]
            }:
                raise ValueError
            unsigned = dict(manifest)
            declared_sha = unsigned.pop("manifest_sha256")
            if (
                manifest["manifest_schema_version"] != REAL_RUN_SCHEMA
                or _sha(canonical_json(unsigned)) != declared_sha
            ):
                raise ValueError
            if (
                manifest["source"] != _SOURCE
                or manifest["dataset"] != _DATASET.value
                or manifest["selected_case_ids"] != list(_CASE_IDS)
            ):
                raise ValueError
            identity = ValidationAttemptIdentityV3.from_dict(_mapping(manifest["identity"]))
            lineage = _lineage_from_mapping(_mapping(manifest["lineage"]))
            files = _verified_files(directory=directory, entries=manifest["files"])
            if (
                manifest["attempt_outcome"] != AttemptOutcomeV3.FAILED.value
                or set(files) != _FILES_FAILURE
            ):
                raise ValueError
            envelope_raw = files["authorization/envelope.json"]
            if _sha(envelope_raw) != lineage.authorization_envelope_sha256:
                raise ValueError
            envelope = RealRuntimeAuthorizationEnvelope.from_bytes(envelope_raw)
            if (
                not self._runtime_authorization_verifier.verify_real_file_authorization(envelope)
                or envelope.authorization_key_id != self._authorization_anchor.key_id
                or envelope.authorization.signer_key_id != self._authorization_anchor.key_id
                or envelope.runner_key_id != self._runner_anchor.key_id
                or envelope.authorization.sample_sha256 != self.context.sample.sample_spec_sha256
            ):
                raise ValueError
            plan_raw = files["request/plan.json"]
            if not _plan_sha_matches(plan_raw, lineage.plan_sha256) or not _plan_binds(
                plan_raw, identity, envelope, self.context
            ):
                raise ValueError
            receipt_raw = files["authorization/claim-receipt.json"]
            receipt = RealClaimReceipt.from_bytes(receipt_raw)
            if _sha(receipt_raw) != lineage.claim_receipt_sha256:
                raise ValueError
            if (
                receipt.authorization_id != identity.authorization_id
                or receipt.invocation_id != identity.invocation_id
                or receipt.run_id != identity.run_id
                or receipt.claim_nonce != envelope.claim_nonce
                or receipt.authorization_envelope_sha256 != lineage.authorization_envelope_sha256
                or receipt.plan_sha256 != lineage.plan_sha256
                or receipt.authorization_key_id != envelope.authorization_key_id
                or not self._claim_receipt_verifier.verify(receipt)
            ):
                raise ValueError
            attestation_raw = files["runner/result-attestation.real.v2.json"]
            attestation = RealResultAttestation.from_bytes(attestation_raw)
            if (
                attestation.identity != identity
                or attestation.lineage != lineage
                or attestation.signer_key_id != self._runner_anchor.key_id
                or attestation.signer_key_id != envelope.runner_key_id
                or dict(attestation.versions) != dict(envelope.frozen_versions)
                or not self._runner_anchor.verify(
                    domain=self._DOMAIN,
                    payload=attestation.unsigned_payload(),
                    signature=attestation.signature,
                )
                or attestation.outcome.value != manifest["attempt_outcome"]
                or attestation.started_at.to_iso() != manifest["started_at"]
                or attestation.ended_at.to_iso() != manifest["ended_at"]
                or not (
                    envelope.authorization.valid_from.value
                    <= receipt.claimed_at.value
                    <= attestation.started_at.value
                    < attestation.ended_at.value
                    < envelope.authorization.expires_at.value
                )
            ):
                raise ValueError
            _verify_result_file_shape(files=files, attestation=attestation)
            return VerifiedRealRun(
                identity=identity,
                outcome=attestation.outcome,
                failure=attestation.failure,
                started_at=attestation.started_at,
                ended_at=attestation.ended_at,
                lineage=lineage,
                manifest_sha256=declared_sha,
                attestation_sha256=_sha(attestation_raw),
            )
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise ValidationEvidenceVerificationError(
                "REAL validation evidence verification failed"
            ) from error

    def verify_assessment(self, *, assessment_directory: Path) -> VerifiedRealAssessment:
        try:
            summary_dir = assessment_directory / "_assessment-real-v2"
            manifest = _load_canonical(summary_dir / "manifest.json")
            complete = _load_canonical(summary_dir / "COMPLETE")
            expected = {
                "assessment_schema_version",
                "dataset",
                "manifest_sha256",
                "runs",
                "source",
                "source_dataset_contract",
                "promotion_profile",
                "sample_spec_sha256",
                "sample_spec_version",
            }
            if set(manifest) != expected or complete != {
                "manifest_sha256": manifest["manifest_sha256"]
            }:
                raise ValueError
            unsigned = dict(manifest)
            declared_sha = unsigned.pop("manifest_sha256")
            if (
                _sha(canonical_json(unsigned)) != declared_sha
                or manifest["assessment_schema_version"] != REAL_ASSESSMENT_SCHEMA
            ):
                raise ValueError
            runs_raw = manifest["runs"]
            if not isinstance(runs_raw, list) or not runs_raw:
                raise ValueError
            runs: list[VerifiedRealRun] = []
            for item in runs_raw:
                entry = _mapping(item)
                if set(entry) != {"directory", "identity", "lineage", "manifest_sha256"}:
                    raise ValueError
                run = self.verify_run(directory=assessment_directory / str(entry["directory"]))
                if (
                    run.identity.to_dict() != entry["identity"]
                    or run.lineage.to_dict() != entry["lineage"]
                    or run.manifest_sha256 != entry["manifest_sha256"]
                ):
                    raise ValueError
                runs.append(run)
            expected_unsigned = _assessment_unsigned(runs=tuple(runs), context=self.context)
            if unsigned != expected_unsigned:
                raise ValueError
            assessment = _assessment_from_runs(
                runs=tuple(runs),
                context=self.context,
                evidence_relative_path=summary_dir.relative_to(self._root).as_posix(),
                evidence_sha256=declared_sha,
            )
            return VerifiedRealAssessment(
                assessment=assessment,
                runs=tuple(runs),
                manifest_sha256=declared_sha,
                evidence_relative_path=summary_dir.relative_to(self._root).as_posix(),
            )
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise ValidationEvidenceVerificationError(
                "REAL assessment evidence verification failed"
            ) from error


def _run_directory(root: Path, identity: ValidationAttemptIdentityV3) -> Path:
    return (
        root
        / "source-assessments-real-v2"
        / "xtquant"
        / "trading_status"
        / path_safe_id(str(identity.assessment_id))
        / path_safe_id(str(identity.run_id))
    )


def _assessment_unsigned(
    *, runs: tuple[VerifiedRealRun, ...], context: V3EvidenceTrustContext
) -> dict[str, object]:
    return {
        "assessment_schema_version": REAL_ASSESSMENT_SCHEMA,
        "dataset": _DATASET.value,
        "promotion_profile": context.promotion_profile.to_dict(),
        "runs": [
            {
                "directory": path_safe_id(str(run.identity.run_id)),
                "identity": run.identity.to_dict(),
                "lineage": run.lineage.to_dict(),
                "manifest_sha256": run.manifest_sha256,
            }
            for run in runs
        ],
        "sample_spec_sha256": context.sample.sample_spec_sha256,
        "sample_spec_version": context.sample.sample_spec_version,
        "source": _SOURCE,
        "source_dataset_contract": context.source_dataset_contract.to_dict(),
    }


def _assessment_from_runs(
    *,
    runs: tuple[VerifiedRealRun, ...],
    context: V3EvidenceTrustContext,
    evidence_relative_path: str,
    evidence_sha256: str,
) -> SourceCandidateAssessment:
    if any(run.identity.assessment_id != runs[0].identity.assessment_id for run in runs):
        raise ValueError("REAL assessment crosses assessment identities")
    failures = [run.failure.category for run in runs if run.failure is not None]
    reason = failures[0] if failures else "REAL_SUCCESS_REQUIRES_FORMAL_QUALITY_GATES"
    return SourceCandidateAssessment(
        assessment_id=runs[0].identity.assessment_id,
        source=_SOURCE,
        dataset=_DATASET,
        evidence_kind=EvidenceKind.REAL,
        semantic_contract_verified=False,
        schema_unit_verified=False,
        coverage_adequate=False,
        quality_passed=False,
        fixed_sample_differences_adequate=False,
        stability_runs=len(runs),
        status=AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION,
        blocking_reason=reason,
        assessment_config_version=context.promotion_profile.promotion_config_version,
        sample_spec_version=context.sample.sample_spec_version,
        rule_version="quality-rules.v1",
        evidence_relative_path=evidence_relative_path,
        evidence_sha256=evidence_sha256,
        created_at=max((run.ended_at for run in runs), key=lambda item: item.value),
    )


def _verify_result_file_shape(
    *, files: Mapping[str, bytes], attestation: RealResultAttestation
) -> None:
    if attestation.outcome is AttemptOutcomeV3.FAILED:
        failure = attestation.failure
        if failure is None:
            raise ValueError("REAL failure evidence is invalid")
        expected = {
            "failure": {
                "category": failure.category,
                "retryable": failure.retryable,
            }
        }
        if _load_canonical_bytes(files["failure/attempt.json"]) != expected:
            raise ValueError("REAL failure evidence is invalid")
    else:
        raise ValueError("C1A REAL success evidence is forbidden")


def _plan_binds(
    payload: bytes,
    identity: ValidationAttemptIdentityV3,
    envelope: RealRuntimeAuthorizationEnvelope,
    context: V3EvidenceTrustContext,
) -> bool:
    raw = _load_canonical_bytes(payload)
    expected = {
        "case_ids",
        "dataset",
        "identity",
        "output_scope",
        "plan_sha256",
        "promotion_profile",
        "runner_key_id",
        "sample_spec_sha256",
        "source",
        "source_dataset_contract",
        "versions",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        return False
    unsigned = dict(raw)
    declared = unsigned.pop("plan_sha256")
    return (
        isinstance(declared, str)
        and _sha(canonical_json(unsigned)) == declared == envelope.plan_sha256
        and raw["identity"] == identity.to_dict()
        and raw["source"] == _SOURCE
        and raw["dataset"] == _DATASET.value
        and raw["case_ids"] == list(_CASE_IDS)
        and raw["output_scope"] == envelope.output_scope
        and raw["runner_key_id"] == envelope.runner_key_id
        and raw["sample_spec_sha256"] == context.sample.sample_spec_sha256
        and raw["sample_spec_sha256"] == envelope.authorization.sample_sha256
        and raw["source_dataset_contract"] == context.source_dataset_contract.to_dict()
        and raw["promotion_profile"] == context.promotion_profile.to_dict()
        and raw["versions"] == dict(envelope.frozen_versions)
    )


def _plan_sha_matches(payload: bytes, expected_sha: str) -> bool:
    try:
        raw = _load_canonical_bytes(payload)
        if not isinstance(raw, dict) or not isinstance(raw.get("plan_sha256"), str):
            return False
        unsigned = dict(raw)
        declared = unsigned.pop("plan_sha256")
        return declared == expected_sha and _sha(canonical_json(unsigned)) == expected_sha
    except (TypeError, ValueError):
        return False


def _verified_files(*, directory: Path, entries: object) -> dict[str, bytes]:
    if not isinstance(entries, list):
        raise ValueError
    files: dict[str, bytes] = {}
    for item in entries:
        entry = _mapping(item)
        if set(entry) != {"relative_path", "sha256", "size"} or not isinstance(
            entry["relative_path"], str
        ):
            raise ValueError
        name = entry["relative_path"]
        if name in files or name.startswith("/") or ".." in Path(name).parts:
            raise ValueError
        payload = (directory / name).read_bytes()
        if _sha(payload) != entry["sha256"] or len(payload) != entry["size"]:
            raise ValueError
        files[name] = payload
    actual = {
        path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()
    } - {"manifest.json", "COMPLETE"}
    if actual != set(files):
        raise ValueError
    return files


def _lineage_from_mapping(raw: Mapping[str, object]) -> RealRunLineage:
    if set(raw) != {"authorization_envelope_sha256", "claim_receipt_sha256", "plan_sha256"}:
        raise ValueError
    return RealRunLineage(
        authorization_envelope_sha256=str(raw["authorization_envelope_sha256"]),
        plan_sha256=str(raw["plan_sha256"]),
        claim_receipt_sha256=str(raw["claim_receipt_sha256"]),
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError
    return value


def _load_canonical(path: Path) -> Mapping[str, object]:
    return _mapping(_load_canonical_bytes(path.read_bytes()))


def _load_canonical_bytes(payload: bytes) -> object:
    raw = json.loads(payload)
    if canonical_json(raw) != payload:
        raise ValueError
    return raw


def _validate_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("REAL validation root must be absolute")
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        if current.exists() and (current.is_symlink() or is_reparse_point(current)):
            raise ValueError("REAL validation root is unsafe")
    return root.resolve(strict=False)


__all__ = [
    "REAL_ASSESSMENT_SCHEMA",
    "REAL_RESULT_ATTESTATION_SCHEMA",
    "REAL_RUN_SCHEMA",
    "RealResultAttestation",
    "RealRunInputs",
    "RealRunLineage",
    "RealValidationEvidenceVerifier",
    "RealValidationEvidenceWriter",
    "VerifiedRealAssessment",
    "VerifiedRealRun",
    "canonical_json",
]
