"""Directory-authoritative source-assessment persistence application service."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Self

from quantking.common.errors import InvalidDataTransitionError
from quantking.data.adapters.warehouse.manifest import compute_sha256
from quantking.data.assessment.models import SourceCandidateAssessment
from quantking.data.validation.artifacts import ValidationEvidenceVerifier
from quantking.data.validation.authorization import (
    ResultAttestation,
    ResultAttestationSigner,
    SignerProfile,
    SignerTrustPolicy,
)
from quantking.data.validation.models import VerifiedAssessmentEvidence


@dataclass(frozen=True, slots=True)
class _AssessmentPersistenceRecord:
    """Internal DTO produced only after two complete directory verifications."""

    evidence: VerifiedAssessmentEvidence
    signer_profiles: tuple[str, ...]
    result_attestation_sha256s: tuple[str, ...]
    receipt_sha256: str


class _AssessmentRepository(Protocol):
    def _persist_verified_source_assessment(
        self, record: _AssessmentPersistenceRecord
    ) -> SourceCandidateAssessment: ...


class _AssessmentUnitOfWork(Protocol):
    data: _AssessmentRepository

    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def commit(self) -> None: ...


class _AssessmentUnitOfWorkFactory(Protocol):
    def __call__(
        self, *, assessment_persistence_key: bytes | None = None
    ) -> _AssessmentUnitOfWork: ...


class VerifiedSourceAssessmentService:
    """Re-read managed evidence and persist only the verifier's exact result."""

    def __init__(
        self,
        *,
        validation_root: Path,
        signer: ResultAttestationSigner,
        signer_trust_policy: SignerTrustPolicy,
        uow_factory: _AssessmentUnitOfWorkFactory,
    ) -> None:
        self._root = validation_root
        self._policy = signer_trust_policy
        self._uow_factory = uow_factory
        self._persistence_key = secrets.token_bytes(32)
        self._verifier = ValidationEvidenceVerifier(
            validation_root=validation_root,
            runner_attestation_verifier=signer,
            signer_trust_policy=signer_trust_policy,
        )

    def persist(self, assessment_directory: Path) -> SourceCandidateAssessment:
        directory = self._managed_assessment_directory(assessment_directory)
        first = self._verifier.verify_assessment(assessment_directory=directory)
        first_summary = self._attestation_summary(directory, first)
        with self._uow_factory(assessment_persistence_key=self._persistence_key) as uow:
            second = self._verifier.verify_assessment(assessment_directory=directory)
            second_summary = self._attestation_summary(directory, second)
            if first != second or first_summary != second_summary:
                raise InvalidDataTransitionError(
                    "assessment evidence changed between verification and persistence"
                )
            unsigned = _AssessmentPersistenceRecord(
                evidence=second,
                signer_profiles=second_summary[0],
                result_attestation_sha256s=second_summary[1],
                receipt_sha256="0" * 64,
            )
            record = _AssessmentPersistenceRecord(
                evidence=unsigned.evidence,
                signer_profiles=unsigned.signer_profiles,
                result_attestation_sha256s=unsigned.result_attestation_sha256s,
                receipt_sha256=_record_receipt(unsigned, self._persistence_key),
            )
            saved = uow.data._persist_verified_source_assessment(record)
            uow.commit()
        return saved

    def _managed_assessment_directory(self, directory: Path) -> Path:
        if not isinstance(directory, Path) or not directory.is_absolute():
            raise ValueError("assessment directory must be an absolute managed path")
        try:
            relative = directory.relative_to(self._root)
        except ValueError as error:
            raise ValueError("assessment directory is outside the managed root") from error
        if not relative.parts or not directory.is_dir():
            raise ValueError("assessment directory is missing")
        return directory

    def _attestation_summary(
        self,
        directory: Path,
        evidence: VerifiedAssessmentEvidence,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        manifest = json.loads((directory / "_assessment" / "manifest.json").read_bytes())
        entries = manifest.get("runs")
        if not isinstance(entries, list) or len(entries) != len(evidence.verified_runs):
            raise InvalidDataTransitionError("assessment run summary is invalid")
        profiles: set[str] = set()
        hashes: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise InvalidDataTransitionError("assessment run summary is invalid")
            path = directory / str(entry["directory"]) / "runner" / "result-attestation.json"
            payload = path.read_bytes()
            attestation = ResultAttestation.from_bytes(payload)
            profile = self._policy.profile_for(attestation.signer_key_id)
            if profile is None:
                raise InvalidDataTransitionError("result attestation signer is not allowlisted")
            profiles.add(profile.value)
            hashes.append(compute_sha256(payload))
        if SignerProfile.REAL.value in profiles:
            raise InvalidDataTransitionError("S1-07A cannot persist REAL signer evidence")
        return tuple(sorted(profiles)), tuple(sorted(hashes))


def _record_receipt(record: _AssessmentPersistenceRecord, key: bytes) -> str:
    payload = json.dumps(
        {
            "assessment_id": str(record.evidence.assessment.assessment_id),
            "evidence_relative_path": record.evidence.evidence_relative_path,
            "evidence_sha256": record.evidence.evidence_manifest_sha256,
            "result_attestation_sha256s": list(record.result_attestation_sha256s),
            "signer_profiles": list(record.signer_profiles),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _record_receipt_is_valid(record: _AssessmentPersistenceRecord, key: bytes | None) -> bool:
    return key is not None and hmac.compare_digest(
        record.receipt_sha256, _record_receipt(record, key)
    )


__all__ = [
    "VerifiedSourceAssessmentService",
]
