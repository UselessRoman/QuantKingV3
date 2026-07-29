"""Directory-authoritative SQLite persistence for verified source assessments.

This module is the sole assessment write adapter.  Its public API accepts only
an absolute evidence directory and keeps that directory locked from the final
verification until the database commit completes.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from contextlib import AbstractContextManager, suppress
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from quantking.common.errors import InvalidDataTransitionError
from quantking.data.adapters.persistence.models import SourceCandidateAssessmentRow
from quantking.data.adapters.persistence.sqlite import (
    _assessment_row_values,
    _row_matches,
)
from quantking.data.adapters.warehouse.manifest import compute_sha256
from quantking.data.assessment.models import (
    AssessmentStatus,
    EvidenceKind,
    SourceCandidateAssessment,
)
from quantking.data.validation.artifacts import ValidationEvidenceVerifier
from quantking.data.validation.authorization import (
    ResultAttestation,
    ResultAttestationSigner,
    SignerProfile,
    SignerTrustPolicy,
)
from quantking.data.validation.models import VerifiedAssessmentEvidence
from quantking.data.validation.samples import load_packaged_fixed_sample
from quantking.data.validation.service import assess_verified_runs, common_fingerprint

_Summary = tuple[tuple[str, ...], tuple[str, ...]]
_VerifiedSnapshot = tuple[VerifiedAssessmentEvidence, _Summary]


class _AssessmentDirectoryLock(AbstractContextManager["_AssessmentDirectoryLock"]):
    """Cross-process writer lock plus non-shareable handles for existing files."""

    def __init__(self, *, root: Path, directory: Path) -> None:
        relative = directory.relative_to(root).as_posix()
        lock_root = root / ".assessment-persistence-locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        self._reject_reparse(lock_root, root)
        name = hashlib.sha256(relative.encode()).hexdigest() + ".lock"
        self._path = lock_root / name
        self._descriptor: int | None = None
        self._handles: list[int] = []
        self._streams: list[BinaryIO] = []
        self._directory = directory

    def __enter__(self) -> _AssessmentDirectoryLock:
        try:
            self._descriptor = os.open(
                self._path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as error:
            raise InvalidDataTransitionError(
                "assessment persistence lock is already held"
            ) from error
        try:
            os.write(self._descriptor, b"ASSESSMENT_PERSISTENCE_LOCK\n")
            os.fsync(self._descriptor)
            for path in self._evidence_files():
                self._hold_file(path)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if os.name == "nt":
            close_handle = ctypes.windll.kernel32.CloseHandle
            for handle in reversed(self._handles):
                close_handle(handle)
            self._handles.clear()
        for stream in reversed(self._streams):
            stream.close()
        self._streams.clear()
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        with suppress(FileNotFoundError):
            self._path.unlink()

    def _hold_file(self, path: Path) -> None:
        if os.name != "nt":
            stream = path.open("rb")
            try:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_SH)  # type: ignore[attr-defined]
            except ImportError:
                pass
            self._streams.append(stream)
            return
        kernel32 = ctypes.windll.kernel32
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            str(path),
            0x80000000,  # GENERIC_READ
            0x00000001,  # FILE_SHARE_READ: deliberately excludes WRITE/DELETE
            None,
            3,  # OPEN_EXISTING
            0x00000080,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            raise OSError(ctypes.get_last_error(), "cannot lock assessment evidence file")
        self._handles.append(int(handle))

    def _evidence_files(self) -> list[Path]:
        files: list[Path] = []
        pending = [self._directory]
        while pending:
            parent = pending.pop()
            with os.scandir(parent) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if _is_reparse(path):
                        raise InvalidDataTransitionError(
                            "assessment evidence contains an unsafe reparse point"
                        )
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        files.append(path)
        return sorted(files)

    @staticmethod
    def _reject_reparse(path: Path, stop: Path) -> None:
        current = path
        while True:
            if _is_reparse(current):
                raise InvalidDataTransitionError("assessment lock root is an unsafe reparse point")
            if current == stop:
                return
            if current.parent == current:
                raise InvalidDataTransitionError("assessment lock root escaped managed root")
            current = current.parent


class SqliteSourceAssessmentPersistenceAdapter:
    """Persist the exact deterministic result of a locked deep verification."""

    def __init__(
        self,
        *,
        engine: Engine,
        validation_root: Path,
        signer: ResultAttestationSigner,
        signer_trust_policy: SignerTrustPolicy,
    ) -> None:
        self._root = validation_root.resolve(strict=False)
        self._policy = signer_trust_policy
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        self._verifier = ValidationEvidenceVerifier(
            validation_root=self._root,
            runner_attestation_verifier=signer,
            signer_trust_policy=signer_trust_policy,
        )

    def persist_from_directory(
        self, absolute_assessment_directory: Path
    ) -> SourceCandidateAssessment:
        """Verify, lock and atomically persist one managed assessment directory."""
        directory = self._managed_assessment_directory(absolute_assessment_directory)
        with _AssessmentDirectoryLock(root=self._root, directory=directory):
            first = self._snapshot(directory)
            final = self._verify_final(directory, first)
            session = self._session_factory()
            try:
                saved = self._insert_assessment(session, *final)
                self._verify_unchanged(directory, final)
                self._commit(session, directory, final)
            except BaseException:
                session.rollback()
                raise
            finally:
                session.close()
            return saved

    def _managed_assessment_directory(self, directory: Path) -> Path:
        if not isinstance(directory, Path) or not directory.is_absolute():
            raise ValueError("assessment directory must be an absolute managed path")
        resolved = directory.resolve(strict=False)
        try:
            relative = resolved.relative_to(self._root)
        except ValueError as error:
            raise ValueError("assessment directory is outside the managed root") from error
        if not relative.parts or not directory.is_dir():
            raise ValueError("assessment directory is missing")
        current = directory
        while True:
            if _is_reparse(current):
                raise ValueError("assessment directory contains an unsafe reparse point")
            if current.resolve(strict=False) == self._root:
                break
            if current.parent == current:
                raise ValueError("assessment directory is outside the managed root")
            current = current.parent
        return resolved

    def _snapshot(self, directory: Path) -> _VerifiedSnapshot:
        evidence = self._verifier.verify_assessment(assessment_directory=directory)
        return evidence, self._attestation_summary(directory, evidence)

    def _verify_final(self, directory: Path, expected: _VerifiedSnapshot) -> _VerifiedSnapshot:
        actual = self._snapshot(directory)
        if actual != expected:
            raise InvalidDataTransitionError(
                "assessment evidence changed between verification and persistence"
            )
        return actual

    def _verify_unchanged(self, directory: Path, expected: _VerifiedSnapshot) -> None:
        if self._snapshot(directory) != expected:
            raise InvalidDataTransitionError("assessment evidence changed before database commit")

    def _insert_assessment(
        self,
        session: Session,
        evidence: VerifiedAssessmentEvidence,
        summary: _Summary,
    ) -> SourceCandidateAssessment:
        self._validate_persistable(evidence, summary)
        assessment = evidence.assessment
        values = {
            **_assessment_row_values(assessment),
            "signer_profiles_json": json.dumps(
                list(summary[0]), separators=(",", ":"), sort_keys=True
            ),
            "result_attestation_sha256s_json": json.dumps(
                list(summary[1]), separators=(",", ":"), sort_keys=True
            ),
        }
        row = session.get(SourceCandidateAssessmentRow, str(assessment.assessment_id))
        if row is not None and not _row_matches(row, values):
            raise InvalidDataTransitionError("source assessment evidence is immutable")
        if row is None:
            session.add(SourceCandidateAssessmentRow(**values))
        session.flush()
        return assessment

    def _commit(
        self,
        session: Session,
        directory: Path,
        expected: _VerifiedSnapshot,
    ) -> None:
        self._verify_unchanged(directory, expected)
        session.commit()

    def _validate_persistable(
        self, evidence: VerifiedAssessmentEvidence, summary: _Summary
    ) -> None:
        assessment = evidence.assessment
        if (
            assessment.evidence_sha256 != evidence.evidence_manifest_sha256
            or assessment.evidence_relative_path != evidence.evidence_relative_path
        ):
            raise InvalidDataTransitionError(
                "source assessment does not bind the verified evidence artifact"
            )
        expected = assess_verified_runs(
            verified_runs=evidence.verified_runs,
            evidence_relative_path=evidence.evidence_relative_path,
            evidence_sha256=evidence.evidence_manifest_sha256,
            created_at=assessment.created_at,
            sample_eligibility=evidence.sample_eligibility,
        )
        controlled_sample = load_packaged_fixed_sample()
        if (
            evidence.sample_eligibility is not controlled_sample.eligibility
            or assessment.sample_spec_version != controlled_sample.sample_spec_version
            or any(
                run.sample_spec_sha256 != controlled_sample.sample_spec_sha256
                or run.versions.sample_spec_version != controlled_sample.sample_spec_version
                for run in evidence.verified_runs
            )
        ):
            raise InvalidDataTransitionError(
                "source assessment does not bind the controlled fixed sample"
            )
        if expected != assessment:
            raise InvalidDataTransitionError(
                "source assessment is not the deterministic result of verified runs"
            )
        if (
            assessment.evidence_kind is not EvidenceKind.SYNTHETIC
            or assessment.status is not AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION
        ):
            raise InvalidDataTransitionError(
                "S1-07A assessment persistence accepts only synthetic blocked evidence"
            )
        if evidence.common_run_fingerprint != common_fingerprint(evidence.verified_runs):
            raise InvalidDataTransitionError("source assessment run fingerprint mismatch")
        if summary[0] != (SignerProfile.SYNTHETIC.value,) or (
            len(summary[1]) != len(evidence.verified_runs)
        ):
            raise InvalidDataTransitionError(
                "source assessment signer/attestation summary is incomplete"
            )

    def _attestation_summary(
        self,
        directory: Path,
        evidence: VerifiedAssessmentEvidence,
    ) -> _Summary:
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
        if profiles != {SignerProfile.SYNTHETIC.value}:
            raise InvalidDataTransitionError("S1-07A cannot persist REAL signer evidence")
        return tuple(sorted(profiles)), tuple(sorted(hashes))


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    return os.name == "nt" and bool(os.lstat(path).st_file_attributes & 0x400)


__all__ = ["SqliteSourceAssessmentPersistenceAdapter"]
