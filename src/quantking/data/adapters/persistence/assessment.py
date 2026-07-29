"""Atomic sealed-evidence persistence for source assessments.

The only public write method accepts a managed assessment directory.  The
directory is atomically claimed, copied into a single immutable seal, deeply
verified from that seal, and inserted through a one-shot connection-local
SQLite gate.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
import zipfile
from contextlib import AbstractContextManager, suppress
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from quantking.common.errors import InvalidDataTransitionError
from quantking.data.adapters.persistence.models import SourceCandidateAssessmentRow
from quantking.data.adapters.persistence.sqlite import (
    _assessment_domain,
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

_TRUST_STATUS = "SEALED_VERIFIED_V1"
_WRITE_GATE_NAME = "quantking_assessment_write_gate"
_WRITE_GATE_ARGUMENTS = 23
_Summary = tuple[tuple[str, ...], tuple[str, ...]]
_VerifiedSnapshot = tuple[VerifiedAssessmentEvidence, _Summary]


class AssessmentReadVerifier(Protocol):
    """Read-side trust port consumed by the generic read-only repository."""

    def verify_persisted_row(
        self, row: SourceCandidateAssessmentRow
    ) -> SourceCandidateAssessment: ...


class _ExclusiveFile(AbstractContextManager["_ExclusiveFile"]):
    """Hold one file without WRITE/DELETE sharing until explicitly released."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: int | None = None
        self._stream: BinaryIO | None = None

    def __enter__(self) -> _ExclusiveFile:
        if os.name != "nt":
            self._stream = self._path.open("rb")
            try:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    self._stream.fileno(),
                    fcntl.LOCK_SH,  # type: ignore[attr-defined]
                )
            except ImportError:
                pass
            return self
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
            str(self._path),
            0x80000000,
            0x00000001,
            None,
            3,
            0x00000080,
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            raise OSError("assessment seal cannot be locked")
        self._handle = int(handle)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._handle is not None:
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._stream is not None:
            self._stream.close()
            self._stream = None


class _AssessmentClaim(AbstractContextManager["_AssessmentClaim"]):
    """Atomically remove the caller-visible directory and reserve its name."""

    def __init__(self, *, root: Path, directory: Path, relative: Path) -> None:
        self.operation_id = uuid.uuid4().hex
        self._root = root
        self._directory = directory
        self._relative = relative
        lock_root = root / ".assessment-persistence-locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        _reject_reparse_chain(lock_root, root)
        lock_name = hashlib.sha256(relative.as_posix().encode()).hexdigest() + ".lock"
        self._lock_path = lock_root / lock_name
        self._lock_descriptor: int | None = None
        self._sentinel_descriptor: int | None = None
        self.retained_root = root / ".assessment-retained" / self.operation_id
        self.retained_directory = self.retained_root / relative

    def __enter__(self) -> _AssessmentClaim:
        try:
            self._lock_descriptor = os.open(
                self._lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as error:
            raise InvalidDataTransitionError(
                "assessment persistence is already in progress"
            ) from error
        try:
            os.write(self._lock_descriptor, b"ASSESSMENT_PERSISTENCE_LOCK\n")
            os.fsync(self._lock_descriptor)
            self.retained_directory.parent.mkdir(parents=True, exist_ok=False)
            os.replace(self._directory, self.retained_directory)
            try:
                self._sentinel_descriptor = os.open(
                    self._directory,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o400,
                )
            except BaseException:
                if not self._directory.exists():
                    os.replace(self.retained_directory, self._directory)
                raise
            os.write(self._sentinel_descriptor, b"SEALED_ASSESSMENT_V1\n")
            os.fsync(self._sentinel_descriptor)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._sentinel_descriptor is not None:
            os.close(self._sentinel_descriptor)
            self._sentinel_descriptor = None
        if self._lock_descriptor is not None:
            os.close(self._lock_descriptor)
            self._lock_descriptor = None
        with suppress(FileNotFoundError):
            self._lock_path.unlink()


class _SQLiteWriteGate:
    """One row, one connection, one transaction, one short-lived use."""

    def __init__(self) -> None:
        self._expected: tuple[object, ...] | None = None
        self._deadline = 0.0
        self._consumed = False

    def arm(self, expected: tuple[object, ...]) -> None:
        if self._expected is not None:
            raise InvalidDataTransitionError("assessment write gate is already active")
        self._expected = expected
        self._deadline = time.monotonic() + 10.0
        self._consumed = False

    def __call__(self, *actual: object) -> int:
        if (
            self._expected is None
            or self._consumed
            or time.monotonic() > self._deadline
            or tuple(actual) != self._expected
        ):
            return 0
        self._consumed = True
        return 1

    def assert_consumed(self) -> None:
        if not self._consumed:
            raise InvalidDataTransitionError("assessment write gate was not consumed")

    def disarm(self) -> None:
        self._expected = None
        self._deadline = 0.0
        self._consumed = True


class SealedAssessmentReadVerifier:
    """Fail-closed deep verifier for rows returned by generic read repositories."""

    def __init__(
        self,
        *,
        validation_root: Path,
        signer: ResultAttestationSigner,
        signer_trust_policy: SignerTrustPolicy,
    ) -> None:
        self._root = validation_root.resolve(strict=False)
        self._signer = signer
        self._policy = signer_trust_policy

    def verify_persisted_row(self, row: SourceCandidateAssessmentRow) -> SourceCandidateAssessment:
        try:
            if row.persistence_trust_status != _TRUST_STATUS:
                raise ValueError("untrusted assessment row")
            seal = _resolve_managed_file(self._root, row.sealed_evidence_relative_path)
            if compute_sha256(seal.read_bytes()) != row.sealed_evidence_sha256:
                raise ValueError("assessment seal mismatch")
            expected = _assessment_domain(row)
            evidence, summary = _verify_sealed_archive(
                seal=seal,
                original_relative=Path(expected.evidence_relative_path).parent,
                signer=self._signer,
                policy=self._policy,
                workspace=self._root,
            )
            if (
                evidence.assessment != expected
                or evidence.evidence_manifest_sha256 != row.evidence_sha256
                or summary[0] != tuple(json.loads(row.signer_profiles_json))
                or summary[1] != tuple(json.loads(row.result_attestation_sha256s_json))
            ):
                raise ValueError("persisted assessment lineage mismatch")
            return expected
        except Exception:
            raise InvalidDataTransitionError("persisted assessment evidence is invalid") from None


class SqliteSourceAssessmentPersistenceAdapter:
    """The sole write adapter for verified source assessments."""

    def __init__(
        self,
        *,
        engine: Engine,
        validation_root: Path,
        signer: ResultAttestationSigner,
        signer_trust_policy: SignerTrustPolicy,
    ) -> None:
        self._root = validation_root.resolve(strict=False)
        self._signer = signer
        self._policy = signer_trust_policy
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)
        self._reader = SealedAssessmentReadVerifier(
            validation_root=self._root,
            signer=signer,
            signer_trust_policy=signer_trust_policy,
        )

    def persist_from_directory(
        self, absolute_assessment_directory: Path
    ) -> SourceCandidateAssessment:
        """Atomically seal, deeply verify and persist one assessment."""
        directory, relative = self._managed_path(absolute_assessment_directory)
        if not directory.is_dir():
            existing = self._existing_for_missing_path(relative)
            if existing is not None:
                return existing
            raise ValueError("assessment directory is missing")
        preliminary = self._snapshot(
            root=self._root,
            directory=directory,
        )
        with _AssessmentClaim(
            root=self._root,
            directory=directory,
            relative=relative,
        ) as claim:
            try:
                claimed = self._snapshot(
                    root=claim.retained_root,
                    directory=claim.retained_directory,
                )
                if claimed != preliminary:
                    raise InvalidDataTransitionError(
                        "assessment evidence changed during atomic claim"
                    )
                incomplete = self._build_seal(
                    directory=claim.retained_directory,
                    operation_id=claim.operation_id,
                )
                final = self._verify_final(incomplete, relative, claimed)
                seal, seal_sha = self._finalize_seal(incomplete)
                with _ExclusiveFile(seal):
                    locked_sha = compute_sha256(seal.read_bytes())
                    if locked_sha != seal_sha:
                        raise InvalidDataTransitionError(
                            "finalized assessment seal changed before lock"
                        )
                    locked_final = self._verify_final(seal, relative, claimed)
                    if locked_final != final:
                        raise InvalidDataTransitionError(
                            "finalized assessment seal verification drifted"
                        )
                    with suppress(FileNotFoundError):
                        incomplete.unlink()
                    saved = self._persist_transaction(
                        evidence=locked_final[0],
                        summary=locked_final[1],
                        seal=seal,
                        seal_sha=locked_sha,
                    )
                    self._verify_unchanged(seal, locked_sha)
                return saved
            except InvalidDataTransitionError:
                self._record_failure(claim.operation_id)
                raise
            except Exception:
                self._record_failure(claim.operation_id)
                raise InvalidDataTransitionError("assessment persistence failed safely") from None

    def _managed_path(self, directory: Path) -> tuple[Path, Path]:
        if not isinstance(directory, Path) or not directory.is_absolute():
            raise ValueError("assessment directory must be an absolute managed path")
        try:
            relative = directory.relative_to(self._root)
        except ValueError as error:
            raise ValueError("assessment directory is outside the managed root") from error
        if not relative.parts:
            raise ValueError("assessment directory is missing")
        _reject_reparse_chain(directory.parent, self._root)
        if directory.exists() and _is_reparse(directory):
            raise ValueError("assessment directory contains an unsafe reparse point")
        return directory, relative

    def _existing_for_missing_path(self, relative: Path) -> SourceCandidateAssessment | None:
        evidence_relative = (relative / "_assessment").as_posix()
        session = self._session_factory()
        try:
            row = (
                session.query(SourceCandidateAssessmentRow)
                .filter_by(evidence_relative_path=evidence_relative)
                .one_or_none()
            )
            return self._reader.verify_persisted_row(row) if row is not None else None
        finally:
            session.close()

    def _snapshot(self, *, root: Path, directory: Path) -> _VerifiedSnapshot:
        verifier = ValidationEvidenceVerifier(
            validation_root=root,
            runner_attestation_verifier=self._signer,
            signer_trust_policy=self._policy,
        )
        evidence = verifier.verify_assessment(assessment_directory=directory)
        _verify_exact_assessment_tree(directory)
        return evidence, _attestation_summary(self._policy, directory, evidence)

    def _build_seal(self, *, directory: Path, operation_id: str) -> Path:
        seal_root = self._root / ".assessment-seals"
        incomplete_root = seal_root / ".incomplete"
        incomplete_root.mkdir(parents=True, exist_ok=True)
        _reject_reparse_chain(incomplete_root, self._root)
        target = incomplete_root / f"{operation_id}.zip"
        files = _evidence_files(directory)
        with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_STORED) as archive:
            for path in files:
                archive.write(path, path.relative_to(directory).as_posix())
        _fsync_file(target)
        return target

    def _verify_final(
        self,
        seal: Path,
        original_relative: Path,
        expected: _VerifiedSnapshot,
    ) -> _VerifiedSnapshot:
        actual = _verify_sealed_archive(
            seal=seal,
            original_relative=original_relative,
            signer=self._signer,
            policy=self._policy,
            workspace=self._root,
        )
        if actual != expected:
            raise InvalidDataTransitionError("assessment evidence changed before atomic sealing")
        return actual

    def _finalize_seal(self, incomplete: Path) -> tuple[Path, str]:
        payload = incomplete.read_bytes()
        sha = compute_sha256(payload)
        seal_root = self._root / ".assessment-seals"
        target = seal_root / f"{sha}.zip"
        if target.exists():
            return target, sha
        os.replace(incomplete, target)
        return target, sha

    def _persist_transaction(
        self,
        *,
        evidence: VerifiedAssessmentEvidence,
        summary: _Summary,
        seal: Path,
        seal_sha: str,
    ) -> SourceCandidateAssessment:
        self._validate_persistable(evidence, summary)
        assessment = evidence.assessment
        seal_relative = seal.relative_to(self._root).as_posix()
        values = {
            **_assessment_row_values(assessment),
            "signer_profiles_json": json.dumps(
                list(summary[0]), separators=(",", ":"), sort_keys=True
            ),
            "result_attestation_sha256s_json": json.dumps(
                list(summary[1]), separators=(",", ":"), sort_keys=True
            ),
            "persistence_trust_status": _TRUST_STATUS,
            "sealed_evidence_relative_path": seal_relative,
            "sealed_evidence_sha256": seal_sha,
        }
        expected_gate = tuple(values[column] for column in _GATE_COLUMNS)
        session = self._session_factory()
        gate = _SQLiteWriteGate()
        try:
            row = session.get(
                SourceCandidateAssessmentRow,
                str(assessment.assessment_id),
            )
            if row is not None:
                if not _row_matches(row, values):
                    raise InvalidDataTransitionError("source assessment evidence is immutable")
                return assessment
            raw = session.connection().connection.driver_connection
            if not isinstance(raw, sqlite3.Connection):
                raise InvalidDataTransitionError("assessment write gate requires SQLite")
            raw.create_function(_WRITE_GATE_NAME, _WRITE_GATE_ARGUMENTS, gate)
            gate.arm(expected_gate)
            saved = self._insert_assessment(session, evidence, values)
            gate.assert_consumed()
            gate.disarm()
            self._verify_unchanged(seal, seal_sha)
            self._commit(session, seal, seal_sha)
            return saved
        except BaseException:
            session.rollback()
            raise
        finally:
            gate.disarm()
            session.close()

    def _insert_assessment(
        self,
        session: Session,
        evidence: VerifiedAssessmentEvidence,
        values: dict[str, object],
    ) -> SourceCandidateAssessment:
        assessment = evidence.assessment
        row = session.get(SourceCandidateAssessmentRow, str(assessment.assessment_id))
        if row is not None and not _row_matches(row, values):
            raise InvalidDataTransitionError("source assessment evidence is immutable")
        if row is None:
            session.add(SourceCandidateAssessmentRow(**values))
            session.flush()
        return assessment

    def _verify_unchanged(self, seal: Path, expected_sha: str) -> None:
        if compute_sha256(seal.read_bytes()) != expected_sha:
            raise InvalidDataTransitionError("assessment seal changed before database commit")

    def _commit(self, session: Session, seal: Path, expected_sha: str) -> None:
        self._verify_unchanged(seal, expected_sha)
        session.commit()

    def _validate_persistable(
        self, evidence: VerifiedAssessmentEvidence, summary: _Summary
    ) -> None:
        assessment = evidence.assessment
        if (
            assessment.evidence_sha256 != evidence.evidence_manifest_sha256
            or assessment.evidence_relative_path != evidence.evidence_relative_path
        ):
            raise InvalidDataTransitionError("source assessment does not bind verified evidence")
        expected = assess_verified_runs(
            verified_runs=evidence.verified_runs,
            evidence_relative_path=evidence.evidence_relative_path,
            evidence_sha256=evidence.evidence_manifest_sha256,
            created_at=assessment.created_at,
            sample_eligibility=evidence.sample_eligibility,
        )
        sample = load_packaged_fixed_sample()
        if (
            evidence.sample_eligibility is not sample.eligibility
            or assessment.sample_spec_version != sample.sample_spec_version
            or any(
                run.sample_spec_sha256 != sample.sample_spec_sha256
                or run.versions.sample_spec_version != sample.sample_spec_version
                for run in evidence.verified_runs
            )
        ):
            raise InvalidDataTransitionError(
                "source assessment does not bind the controlled fixed sample"
            )
        if expected != assessment:
            raise InvalidDataTransitionError(
                "source assessment is not the deterministic verified result"
            )
        if (
            assessment.evidence_kind is not EvidenceKind.SYNTHETIC
            or assessment.status is not AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION
        ):
            raise InvalidDataTransitionError("S1-07A accepts only synthetic blocked evidence")
        if evidence.common_run_fingerprint != common_fingerprint(evidence.verified_runs):
            raise InvalidDataTransitionError("source assessment fingerprint mismatch")
        if summary[0] != (SignerProfile.SYNTHETIC.value,) or len(summary[1]) != len(
            evidence.verified_runs
        ):
            raise InvalidDataTransitionError("source assessment attestation summary is incomplete")

    def _record_failure(self, operation_id: str) -> None:
        failure_root = self._root / ".assessment-failures"
        failure_root.mkdir(parents=True, exist_ok=True)
        target = failure_root / f"{operation_id}.json"
        with suppress(OSError):
            target.write_bytes(
                _canonical(
                    {
                        "category": "ASSESSMENT_PERSISTENCE_FAILED",
                        "retryable": False,
                    }
                )
            )


_GATE_COLUMNS = (
    "assessment_id",
    "source",
    "dataset",
    "evidence_kind",
    "semantic_contract_verified",
    "schema_unit_verified",
    "coverage_adequate",
    "quality_passed",
    "fixed_sample_differences_adequate",
    "stability_runs",
    "status",
    "blocking_reason",
    "assessment_config_version",
    "sample_spec_version",
    "rule_version",
    "evidence_relative_path",
    "evidence_sha256",
    "signer_profiles_json",
    "result_attestation_sha256s_json",
    "created_at",
    "persistence_trust_status",
    "sealed_evidence_relative_path",
    "sealed_evidence_sha256",
)


def _verify_sealed_archive(
    *,
    seal: Path,
    original_relative: Path,
    signer: ResultAttestationSigner,
    policy: SignerTrustPolicy,
    workspace: Path,
) -> _VerifiedSnapshot:
    temporary_root = workspace / ".assessment-verification"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temporary_root) as temporary:
        root = Path(temporary)
        assessment_directory = root / original_relative
        assessment_directory.mkdir(parents=True)
        _extract_seal(seal, assessment_directory)
        verifier = ValidationEvidenceVerifier(
            validation_root=root,
            runner_attestation_verifier=signer,
            signer_trust_policy=policy,
        )
        evidence = verifier.verify_assessment(assessment_directory=assessment_directory)
        _verify_exact_assessment_tree(assessment_directory)
        summary = _attestation_summary(policy, assessment_directory, evidence)
        return evidence, summary


def _extract_seal(seal: Path, target: Path) -> None:
    seen: set[str] = set()
    with zipfile.ZipFile(seal, "r") as archive:
        for info in archive.infolist():
            relative = PurePosixPath(info.filename)
            if (
                info.is_dir()
                or info.filename in seen
                or relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
                or (info.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise InvalidDataTransitionError("assessment seal inventory is invalid")
            seen.add(info.filename)
            destination = target.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(info))


def _verify_exact_assessment_tree(directory: Path) -> None:
    manifest = json.loads((directory / "_assessment" / "manifest.json").read_bytes())
    runs = manifest.get("runs")
    if not isinstance(runs, list):
        raise InvalidDataTransitionError("assessment tree is invalid")
    expected = {"_assessment"}
    for item in runs:
        if not isinstance(item, dict) or not isinstance(item.get("directory"), str):
            raise InvalidDataTransitionError("assessment tree is invalid")
        expected.add(str(item["directory"]))
    actual = {item.name for item in directory.iterdir()}
    if actual != expected or any(not (directory / name).is_dir() for name in expected):
        raise InvalidDataTransitionError("assessment tree contains undeclared entries")
    _evidence_files(directory)


def _evidence_files(directory: Path) -> list[Path]:
    files: list[Path] = []
    pending = [directory]
    while pending:
        parent = pending.pop()
        children = list(os.scandir(parent))
        if parent != directory and not children:
            raise InvalidDataTransitionError("assessment evidence contains an empty directory")
        for entry in children:
            path = Path(entry.path)
            if _is_reparse(path):
                raise InvalidDataTransitionError(
                    "assessment evidence contains an unsafe reparse point"
                )
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                files.append(path)
            else:
                raise InvalidDataTransitionError("assessment evidence entry is invalid")
    return sorted(files)


def _attestation_summary(
    policy: SignerTrustPolicy,
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
        profile = policy.profile_for(attestation.signer_key_id)
        if profile is None:
            raise InvalidDataTransitionError("assessment signer is not allowlisted")
        profiles.add(profile.value)
        hashes.append(compute_sha256(payload))
    if profiles != {SignerProfile.SYNTHETIC.value}:
        raise InvalidDataTransitionError("S1-07A cannot persist REAL evidence")
    return tuple(sorted(profiles)), tuple(sorted(hashes))


def _resolve_managed_file(root: Path, relative: str) -> Path:
    candidate = root / relative
    try:
        candidate.resolve(strict=True).relative_to(root)
    except (FileNotFoundError, ValueError) as error:
        raise ValueError("managed assessment seal is invalid") from error
    _reject_reparse_chain(candidate, root)
    if not candidate.is_file():
        raise ValueError("managed assessment seal is invalid")
    return candidate


def _reject_reparse_chain(path: Path, stop: Path) -> None:
    current = path
    while True:
        if current.exists() and _is_reparse(current):
            raise InvalidDataTransitionError("assessment path contains an unsafe reparse point")
        if current == stop:
            return
        if current.parent == current:
            raise InvalidDataTransitionError("assessment path escaped managed root")
        current = current.parent


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    return os.name == "nt" and bool(os.lstat(path).st_file_attributes & 0x400)


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


__all__ = [
    "AssessmentReadVerifier",
    "SealedAssessmentReadVerifier",
    "SqliteSourceAssessmentPersistenceAdapter",
]
