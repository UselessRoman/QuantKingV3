"""Immutable writer and deep verifier for source-validation evidence."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

from quantking.common.ids import EntityId, parse_entity_id
from quantking.common.paths import is_reparse_point, resolve_under
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.validation_content import (
    verify_canonical_evidence,
    verify_quality_row_evidence,
)
from quantking.data.adapters.warehouse.manifest import (
    WAREHOUSE_COMPLETE_FILENAME,
    WAREHOUSE_MANIFEST_FILENAME,
    compute_sha256,
    path_safe_id,
    validate_managed_relative,
)
from quantking.data.adapters.warehouse.verifier import verify_raw_directory
from quantking.data.assessment.models import EvidenceKind
from quantking.data.comparison.verifier import DifferenceReportVerifier
from quantking.data.models import DatasetKind, QualityRunStatus
from quantking.data.quality.models import (
    QualityOutcomeResult,
    QualityRunArtifact,
    RuleOutcomeStatus,
)
from quantking.data.validation.authorization import (
    ResultAttestation,
    ResultAttestationSigner,
    SignerProfile,
    SignerTrustPolicy,
)
from quantking.data.validation.coverage import evaluate_fixed_sample_coverage
from quantking.data.validation.models import (
    _VERIFICATION_SEAL,
    AssessmentGates,
    RealSourceValidationRun,
    ValidationFailure,
    ValidationRunEvidence,
    ValidationVersions,
    VerifiedAssessmentEvidence,
    VerifiedValidationRun,
)
from quantking.data.validation.quality import validate_quality_rule_catalog
from quantking.data.validation.request import compute_run_fingerprint, verify_request_manifest
from quantking.data.validation.samples import SampleEligibility, load_packaged_fixed_sample
from quantking.data.validation.service import (
    aggregate_gates,
    assess_verified_runs,
    common_fingerprint,
)

if TYPE_CHECKING:
    from quantking.data.validation.artifacts_v3 import (
        V3EvidenceTrustContext,
        ValidationEvidenceVerifierV3,
    )
    from quantking.data.validation.contracts_v3 import V3ResultAttestationSigner

_RUN_SCHEMA = "source-validation-envelope.v2"
_ASSESSMENT_SCHEMA = "source-assessment-manifest.v2"


class ValidationEvidenceConflictError(Exception):
    """An immutable validation ID was reused with different content."""


class ValidationEvidenceVerificationError(Exception):
    """Evidence failed structural or cryptographic verification."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _validate_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("validation_root must be an absolute Path")
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        if current.exists() and is_reparse_point(current):
            raise ValueError("validation_root must not contain a symlink or junction")
    return root


def _run_payload(run: ValidationRunEvidence) -> dict[str, object]:
    return {
        "assessment_id": str(run.assessment_id),
        "authorization_id": str(run.authorization_id),
        "dataset": run.dataset.value,
        "difference_report_id": (
            str(run.difference_report_id) if run.difference_report_id is not None else None
        ),
        "ended_at": run.ended_at.to_iso(),
        "failure": (
            {"category": run.failure.category, "retryable": run.failure.retryable}
            if run.failure is not None
            else None
        ),
        "invocation_id": str(run.invocation_id),
        "request_sha256": run.request_sha256,
        "run_fingerprint": run.run_fingerprint,
        "run_id": str(run.run_id),
        "sample_spec_sha256": run.sample_spec_sha256,
        "source": run.source,
        "started_at": run.started_at.to_iso(),
        "versions": run.versions.to_dict(),
    }


class ValidationEvidenceWriter:
    """Write caller facts and opaque files; never accept or persist verdicts."""

    def __init__(
        self,
        *,
        validation_root: Path,
        runner_attestation_verifier: ResultAttestationSigner | None = None,
    ) -> None:
        self._root = _validate_root(validation_root)
        self._runner_trust = runner_attestation_verifier

    def write(self, *, run: ValidationRunEvidence, files: Mapping[str, tuple[bytes, int]]) -> Path:
        if not isinstance(run, ValidationRunEvidence):
            raise TypeError("writer only accepts an unverified ValidationRunEvidence envelope")
        normalized: list[tuple[str, bytes, int]] = []
        for relative, item in files.items():
            validate_managed_relative(relative, field="validation evidence file")
            payload, row_count = item
            if relative in {WAREHOUSE_MANIFEST_FILENAME, WAREHOUSE_COMPLETE_FILENAME}:
                raise ValueError("reserved validation evidence file name")
            if not isinstance(payload, bytes):
                raise TypeError("validation evidence payload must be bytes")
            if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
                raise ValueError("validation evidence row_count must be non-negative")
            normalized.append((relative, payload, row_count))
        normalized.sort()
        if not normalized:
            raise ValueError("validation evidence files must not be empty")
        file_manifest = [
            {
                "relative_path": relative,
                "row_count": row_count,
                "sha256": compute_sha256(payload),
                "size": len(payload),
            }
            for relative, payload, row_count in normalized
        ]
        unsigned = {
            "files": file_manifest,
            "manifest_schema_version": _RUN_SCHEMA,
            **_run_payload(run),
        }
        manifest_sha = compute_sha256(_canonical(unsigned))
        manifest_bytes = _canonical({**unsigned, "manifest_sha256": manifest_sha})
        complete_bytes = _canonical({"manifest_sha256": manifest_sha})
        directory = self._directory(run)
        if directory.exists():
            if (directory / WAREHOUSE_MANIFEST_FILENAME).read_bytes() == manifest_bytes and (
                directory / WAREHOUSE_COMPLETE_FILENAME
            ).read_bytes() == complete_bytes:
                return directory
            raise ValidationEvidenceConflictError(
                "validation run ID already exists with different content"
            )
        incomplete = directory.with_name(directory.name + ".incomplete")
        incomplete.mkdir(parents=True, exist_ok=False)
        try:
            for relative, payload, _row_count in normalized:
                target = incomplete / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            (incomplete / WAREHOUSE_MANIFEST_FILENAME).write_bytes(manifest_bytes)
            (incomplete / WAREHOUSE_COMPLETE_FILENAME).write_bytes(complete_bytes)
            os.replace(incomplete, directory)
        except Exception:
            if incomplete.exists():
                with suppress(OSError):
                    (incomplete / "failure.json").write_bytes(
                        _canonical({"category": "EVIDENCE_WRITE_FAILURE", "retryable": False})
                    )
            raise
        return directory

    def _directory(self, run: ValidationRunEvidence) -> Path:
        candidate = (
            self._root
            / "source-assessments"
            / run.source.lower()
            / run.dataset.value
            / path_safe_id(str(run.assessment_id))
            / path_safe_id(str(run.run_id))
        )
        try:
            candidate.resolve(strict=False).relative_to(self._root.resolve(strict=False))
        except ValueError as error:
            raise ValueError("validation evidence path escapes the managed root") from error
        return candidate

    def finalize_assessment(self, *, assessment_directory: Path) -> VerifiedAssessmentEvidence:
        verifier = ValidationEvidenceVerifier(
            validation_root=self._root,
            runner_attestation_verifier=self._runner_trust,
        )
        runs = tuple(
            verifier.verify_run(directory=child)
            for child in sorted(assessment_directory.iterdir(), key=lambda item: item.name)
            if child.is_dir()
            and not child.name.startswith("_assessment")
            and not child.name.endswith(".incomplete")
        )
        if not runs:
            raise ValueError("assessment requires at least one completed validation run")
        first = runs[0]
        gates = aggregate_gates(runs)
        unsigned = {
            "assessment_id": str(first.assessment_id),
            "dataset": first.dataset.value,
            "gates": gates.to_dict(),
            "manifest_schema_version": _ASSESSMENT_SCHEMA,
            "run_fingerprint": common_fingerprint(runs),
            "runs": [
                {
                    "directory": path_safe_id(str(run.run_id)),
                    "manifest_sha256": json.loads(
                        (
                            assessment_directory / path_safe_id(str(run.run_id)) / "manifest.json"
                        ).read_bytes()
                    )["manifest_sha256"],
                    "run_id": str(run.run_id),
                }
                for run in runs
            ],
            "sample_eligibility": load_packaged_fixed_sample().eligibility.value,
            "source": first.source,
        }
        manifest_sha = compute_sha256(_canonical(unsigned))
        manifest_bytes = _canonical({**unsigned, "manifest_sha256": manifest_sha})
        complete_bytes = _canonical({"manifest_sha256": manifest_sha})
        directory = assessment_directory / "_assessment"
        if directory.exists():
            if (directory / "manifest.json").read_bytes() != manifest_bytes:
                raise ValidationEvidenceConflictError(
                    "assessment is already finalized with different run evidence"
                )
            return verifier.verify_assessment(assessment_directory=assessment_directory)
        incomplete = assessment_directory / "_assessment.incomplete"
        incomplete.mkdir(parents=False, exist_ok=False)
        (incomplete / "manifest.json").write_bytes(manifest_bytes)
        (incomplete / "COMPLETE").write_bytes(complete_bytes)
        os.replace(incomplete, directory)
        return verifier.verify_assessment(assessment_directory=assessment_directory)


class ValidationEvidenceVerifier:
    def __init__(
        self,
        *,
        validation_root: Path,
        runner_attestation_verifier: ResultAttestationSigner | None = None,
        signer_trust_policy: SignerTrustPolicy | None = None,
        v3_trust_context: object | None = None,
        v3_runner_attestation_verifier: object | None = None,
    ) -> None:
        self._root = _validate_root(validation_root)
        self._runner_trust = runner_attestation_verifier
        self._signer_policy = signer_trust_policy or SignerTrustPolicy.synthetic_only()
        # C0A deliberately accepts no bundled REAL signer.  These opaque,
        # future-C0B injection points only select the separate v3 verifier;
        # v2 trust and byte semantics remain untouched.
        self._v3_trust_context = v3_trust_context
        self._v3_runner_trust = v3_runner_attestation_verifier

    def with_trust_policy(self, policy: SignerTrustPolicy) -> ValidationEvidenceVerifier:
        return ValidationEvidenceVerifier(
            validation_root=self._root,
            runner_attestation_verifier=self._runner_trust,
            signer_trust_policy=policy,
            v3_trust_context=self._v3_trust_context,
            v3_runner_attestation_verifier=self._v3_runner_trust,
        )

    def verify(self, *, directory: Path) -> VerifiedAssessmentEvidence:
        if _schema_version_at(directory / "manifest.json") == "source-validation-envelope.v3":
            return cast(VerifiedAssessmentEvidence, self._v3_verifier().verify(directory=directory))
        run = self.verify_run(directory=directory)
        manifest = json.loads((directory / "manifest.json").read_bytes())
        sha = str(manifest["manifest_sha256"])
        relative = directory.relative_to(self._root).as_posix()
        assessment = assess_verified_runs(
            verified_runs=(run,),
            evidence_relative_path=relative,
            evidence_sha256=sha,
            created_at=run.ended_at,
            sample_eligibility=run.sample_eligibility,
        )
        return VerifiedAssessmentEvidence(
            assessment=assessment,
            verified_runs=(run,),
            evidence_manifest_sha256=sha,
            evidence_relative_path=relative,
            common_run_fingerprint=run.run_fingerprint,
            sample_eligibility=run.sample_eligibility,
            _seal=_VERIFICATION_SEAL,
        )

    def verify_run(self, *, directory: Path) -> VerifiedValidationRun:
        if _schema_version_at(directory / "manifest.json") == "source-validation-envelope.v3":
            return cast(VerifiedValidationRun, self._v3_verifier().verify_run(directory=directory))
        manifest, _embedded, files = self._verify_outer(directory)
        envelope = self._parse_envelope(manifest)
        sample = load_packaged_fixed_sample()
        session_id = EntityId("data_session", envelope.invocation_id.value)
        gates = AssessmentGates.none_passed()
        identity = EvidenceKind.SYNTHETIC
        raw_path: str | None = None
        raw_sha: str | None = None
        canonical_sha: str | None = None
        quality_id: EntityId | None = None
        quality_sha: str | None = None
        difference_sha: str | None = None
        reason: str | None = None
        verification_succeeded = False
        try:
            request = verify_request_manifest(
                files["request/manifest.json"],
                source=envelope.source,
                dataset=envelope.dataset,
                controlled_sample=sample,
            )
            if (
                request.request_sha256 != envelope.request_sha256
                or sample.sample_spec_sha256 != envelope.sample_spec_sha256
                or sample.sample_spec_version != envelope.versions.sample_spec_version
            ):
                raise ValueError("controlled request/sample binding mismatch")
            expected_fingerprint = compute_run_fingerprint(
                source=envelope.source,
                dataset=envelope.dataset,
                request_sha256=request.request_sha256,
                sample_sha256=sample.sample_spec_sha256,
                versions=envelope.versions.to_dict(),
            )
            if expected_fingerprint != envelope.run_fingerprint:
                raise ValueError("run fingerprint mismatch")
            attestation = ResultAttestation.from_bytes(files["runner/result-attestation.json"])
            session_id = attestation.session_id
            if (
                self._runner_trust is None
                or attestation.signer_key_id != self._runner_trust.key_id
                or not self._runner_trust.verify(attestation)
                or attestation.run_id != envelope.run_id
                or attestation.invocation_id != envelope.invocation_id
                or attestation.authorization_id != envelope.authorization_id
                or attestation.source != envelope.source
                or attestation.dataset is not envelope.dataset
                or attestation.request_sha256 != envelope.request_sha256
                or attestation.run_fingerprint != envelope.run_fingerprint
                or attestation.started_at != envelope.started_at
                or attestation.ended_at != envelope.ended_at
            ):
                raise ValueError("result attestation is untrusted or unbound")
            if attestation.evidence_inventory_sha256 != compute_evidence_inventory_sha256(manifest):
                raise ValueError("result attestation artifact lineage mismatch")

            raw_manifests = [
                path
                for path in directory.glob("raw-root/raw/*/*/*/manifest.json")
                if path.is_file()
            ]
            if len(raw_manifests) != 1:
                raise ValueError("exactly one managed Raw manifest is required")
            raw_dir = raw_manifests[0].parent
            raw_verified = verify_raw_directory(raw_dir, data_root=directory / "raw-root")
            raw_manifest = raw_verified.manifest
            if (
                raw_manifest["source"] != envelope.source
                or raw_manifest["dataset"] != envelope.dataset.value
                or raw_manifest["adapter_version"] != envelope.versions.adapter_version
                or raw_manifest["code_version"] != envelope.versions.code_version
                or raw_manifest["request_params"]
                != {
                    "request_sha256": envelope.request_sha256,
                    "run_id": str(envelope.run_id),
                }
            ):
                raise ValueError("Raw lineage does not bind the run/request")
            raw_path = raw_manifests[0].relative_to(directory).as_posix()
            raw_sha = raw_verified.manifest_sha

            canonical_path = directory / "canonical" / "content.parquet"
            canonical = verify_canonical_evidence(path=canonical_path, dataset=envelope.dataset)
            canonical_sha = canonical.content_sha256
            calendar_rows: list[dict[str, object]] = []
            if envelope.dataset is not DatasetKind.EXCHANGE_CALENDAR:
                reference_path = directory / "reference" / "exchange_calendar.parquet"
                if reference_path.is_file():
                    reference = verify_canonical_evidence(
                        path=reference_path,
                        dataset=DatasetKind.EXCHANGE_CALENDAR,
                    )
                    calendar_rows = list(reference.rows)
            coverage = evaluate_fixed_sample_coverage(
                dataset=envelope.dataset,
                rows=list(canonical.rows),
                cases=request.cases,
                calendar_rows=calendar_rows,
            )
            quality_id, quality_sha, quality_ok = _verify_quality(
                directory / "quality-root",
                dataset=envelope.dataset,
                expected_rule=envelope.versions.rule_version,
                expected_schema=envelope.versions.schema_version,
                expected_content_sha=canonical_sha,
            )
            semantic = True
            schema_ok = True

            difference_ok = False
            if envelope.difference_report_id is not None:
                report = DifferenceReportVerifier(
                    artifacts_root=directory / "difference-root"
                ).verify(report_id=envelope.difference_report_id)
                difference_sha = report.manifest_sha256
                difference_ok = (
                    report.dataset == envelope.dataset.value
                    and report.sample_spec_sha256 == sample.sample_spec_sha256
                    and report.threshold_rule_version == envelope.versions.threshold_version
                    and canonical_sha
                    in {report.primary_content_sha256, report.fallback_content_sha256}
                )
            difference_ok = (
                difference_ok and sample.eligibility is SampleEligibility.PRODUCTION_ELIGIBLE
            )
            gates = AssessmentGates(
                semantic_contract_verified=semantic,
                schema_unit_verified=schema_ok,
                coverage_adequate=coverage,
                quality_passed=quality_ok,
                fixed_sample_differences_adequate=difference_ok,
            )
            if (
                attestation.raw_manifest_sha256 != raw_sha
                or attestation.canonical_content_sha256 != canonical_sha
                or attestation.quality_manifest_sha256 != quality_sha
                or attestation.quality_run_id != quality_id
                or attestation.difference_report_id
                != (envelope.difference_report_id if difference_sha else None)
                or attestation.difference_report_sha256 != difference_sha
                or dict(attestation.versions) != envelope.versions.to_dict()
            ):
                raise ValueError("result attestation artifact lineage mismatch")
            verification_succeeded = True
            identity = (
                EvidenceKind.REAL
                if self._signer_policy.profile_for(attestation.signer_key_id) is SignerProfile.REAL
                else EvidenceKind.SYNTHETIC
            )
            if identity is EvidenceKind.SYNTHETIC:
                reason = "SYNTHETIC_RUNNER"
        except Exception as error:
            reason = (
                "RESULT_ATTESTATION_MISMATCH"
                if "attestation artifact lineage mismatch" in str(error)
                else "VERIFICATION_FAILED"
            )
            gates = AssessmentGates.none_passed()
            identity = EvidenceKind.SYNTHETIC

        run_type = (
            RealSourceValidationRun if identity is EvidenceKind.REAL else VerifiedValidationRun
        )
        return run_type(
            run_id=envelope.run_id,
            assessment_id=envelope.assessment_id,
            invocation_id=envelope.invocation_id,
            session_id=session_id,
            authorization_id=envelope.authorization_id,
            source=envelope.source,
            dataset=envelope.dataset,
            versions=envelope.versions,
            request_sha256=envelope.request_sha256,
            sample_spec_sha256=envelope.sample_spec_sha256,
            run_fingerprint=envelope.run_fingerprint,
            evidence_kind=identity,
            gates=gates,
            raw_manifest_relative_path=raw_path,
            raw_manifest_sha256=raw_sha,
            canonical_content_sha256=canonical_sha,
            quality_run_id=quality_id,
            quality_manifest_sha256=quality_sha,
            difference_report_id=envelope.difference_report_id if difference_sha else None,
            difference_report_sha256=difference_sha,
            sample_eligibility=sample.eligibility,
            started_at=envelope.started_at,
            ended_at=envelope.ended_at,
            failure=(
                envelope.failure
                if verification_succeeded
                else ValidationFailure("VERIFICATION_FAILED", False)
            ),
            verification_reason=reason,
            _seal=_VERIFICATION_SEAL,
        )

    def verify_assessment(self, *, assessment_directory: Path) -> VerifiedAssessmentEvidence:
        if (assessment_directory / "_assessment-v3" / "manifest.json").is_file():
            return cast(
                VerifiedAssessmentEvidence,
                self._v3_verifier().verify_assessment(assessment_directory=assessment_directory),
            )
        summary = assessment_directory / "_assessment"
        manifest, embedded = _verify_summary(summary)
        declared = manifest["runs"]
        if not isinstance(declared, list) or not declared:
            raise ValidationEvidenceVerificationError("assessment runs are invalid")
        runs: list[VerifiedValidationRun] = []
        for entry in declared:
            if not isinstance(entry, dict) or set(entry) != {
                "directory",
                "manifest_sha256",
                "run_id",
            }:
                raise ValidationEvidenceVerificationError("assessment run entry is invalid")
            directory = assessment_directory / str(entry["directory"])
            run_manifest = json.loads((directory / "manifest.json").read_bytes())
            if (
                run_manifest.get("manifest_sha256") != entry["manifest_sha256"]
                or run_manifest.get("run_id") != entry["run_id"]
            ):
                raise ValidationEvidenceVerificationError("assessment run lineage mismatch")
            runs.append(self.verify_run(directory=directory))
        run_tuple = tuple(runs)
        sample = load_packaged_fixed_sample()
        if (
            manifest["run_fingerprint"] != common_fingerprint(run_tuple)
            or manifest["sample_eligibility"] != sample.eligibility.value
            or manifest["gates"] != aggregate_gates(run_tuple).to_dict()
        ):
            raise ValidationEvidenceVerificationError(
                "assessment summary does not match deep-verified runs"
            )
        relative = summary.relative_to(self._root).as_posix()
        assessment = assess_verified_runs(
            verified_runs=run_tuple,
            evidence_relative_path=relative,
            evidence_sha256=embedded,
            created_at=max((run.ended_at for run in run_tuple), key=lambda item: item.value),
            sample_eligibility=sample.eligibility,
        )
        if (
            manifest["assessment_id"] != str(assessment.assessment_id)
            or manifest["source"] != assessment.source
            or manifest["dataset"] != assessment.dataset.value
        ):
            raise ValidationEvidenceVerificationError("assessment identity mismatch")
        return VerifiedAssessmentEvidence(
            assessment=assessment,
            verified_runs=run_tuple,
            evidence_manifest_sha256=embedded,
            evidence_relative_path=relative,
            common_run_fingerprint=common_fingerprint(run_tuple),
            sample_eligibility=sample.eligibility,
            _seal=_VERIFICATION_SEAL,
        )

    def _v3_verifier(self) -> ValidationEvidenceVerifierV3:
        if self._v3_trust_context is None or self._v3_runner_trust is None:
            raise ValidationEvidenceVerificationError("v3 trust context is not configured")
        # Import lazily to avoid changing the completed v2 module import graph.
        from quantking.data.validation.artifacts_v3 import ValidationEvidenceVerifierV3

        return ValidationEvidenceVerifierV3(
            validation_root=self._root,
            context=cast("V3EvidenceTrustContext", self._v3_trust_context),
            runner_attestation_verifier=cast("V3ResultAttestationSigner", self._v3_runner_trust),
        )

    def _verify_outer(self, directory: Path) -> tuple[dict[str, object], str, dict[str, bytes]]:
        try:
            resolved = resolve_under(self._root, directory.relative_to(self._root))
        except Exception as error:
            raise ValidationEvidenceVerificationError(
                "validation directory is outside the managed root"
            ) from error
        if not resolved.is_dir() or is_reparse_point(resolved):
            raise ValidationEvidenceVerificationError("validation directory is unsafe")
        manifest_bytes = _read_file(directory / "manifest.json")
        complete_bytes = _read_file(directory / "COMPLETE")
        try:
            manifest = json.loads(manifest_bytes)
            complete = json.loads(complete_bytes)
        except json.JSONDecodeError as error:
            raise ValidationEvidenceVerificationError("manifest or COMPLETE is invalid") from error
        if not isinstance(manifest, dict) or manifest_bytes != _canonical(manifest):
            raise ValidationEvidenceVerificationError("manifest must be canonical JSON")
        unsigned = dict(manifest)
        embedded = unsigned.pop("manifest_sha256", None)
        if (
            manifest.get("manifest_schema_version") != _RUN_SCHEMA
            or not isinstance(embedded, str)
            or compute_sha256(_canonical(unsigned)) != embedded
            or complete != {"manifest_sha256": embedded}
        ):
            raise ValidationEvidenceVerificationError("manifest self-SHA or COMPLETE mismatch")
        expected_fields = {
            "assessment_id",
            "authorization_id",
            "dataset",
            "difference_report_id",
            "ended_at",
            "failure",
            "files",
            "invocation_id",
            "manifest_schema_version",
            "manifest_sha256",
            "request_sha256",
            "run_fingerprint",
            "run_id",
            "sample_spec_sha256",
            "source",
            "started_at",
            "versions",
        }
        if set(manifest) != expected_fields:
            raise ValidationEvidenceVerificationError("manifest fields do not match schema")
        raw_entries = manifest["files"]
        if not isinstance(raw_entries, list):
            raise ValidationEvidenceVerificationError("manifest files must be an array")
        files: dict[str, bytes] = {}
        expected = {"manifest.json", "COMPLETE"}
        for entry in raw_entries:
            if not isinstance(entry, dict) or set(entry) != {
                "relative_path",
                "row_count",
                "sha256",
                "size",
            }:
                raise ValidationEvidenceVerificationError("invalid manifest file entry")
            relative = str(entry["relative_path"])
            try:
                validate_managed_relative(relative, field="validation evidence file")
                payload = _read_file(resolve_under(directory, relative))
            except Exception as error:
                raise ValidationEvidenceVerificationError("unsafe evidence file") from error
            if (
                relative in files
                or len(payload) != entry["size"]
                or compute_sha256(payload) != entry["sha256"]
                or not isinstance(entry["row_count"], int)
                or isinstance(entry["row_count"], bool)
                or entry["row_count"] < 0
            ):
                raise ValidationEvidenceVerificationError("validation evidence file mismatch")
            files[relative] = payload
            expected.add(relative)
        actual = {
            item.relative_to(directory).as_posix()
            for item in directory.rglob("*")
            if item.is_file()
        }
        if actual != expected:
            raise ValidationEvidenceVerificationError("validation evidence file set mismatch")
        return manifest, embedded, files

    @staticmethod
    def _parse_envelope(manifest: Mapping[str, object]) -> ValidationRunEvidence:
        try:
            failure_raw = manifest["failure"]
            failure = (
                None
                if failure_raw is None
                else ValidationFailure(
                    category=str(failure_raw["category"]),  # type: ignore[index]
                    retryable=bool(failure_raw["retryable"]),  # type: ignore[index]
                )
            )
            difference = manifest["difference_report_id"]
            return ValidationRunEvidence(
                run_id=parse_entity_id(str(manifest["run_id"])),
                assessment_id=parse_entity_id(str(manifest["assessment_id"])),
                invocation_id=parse_entity_id(str(manifest["invocation_id"])),
                authorization_id=parse_entity_id(str(manifest["authorization_id"])),
                source=str(manifest["source"]),
                dataset=DatasetKind(str(manifest["dataset"])),
                versions=ValidationVersions(**manifest["versions"]),  # type: ignore[arg-type]
                request_sha256=str(manifest["request_sha256"]),
                sample_spec_sha256=str(manifest["sample_spec_sha256"]),
                run_fingerprint=str(manifest["run_fingerprint"]),
                started_at=UtcTimestamp.from_iso(str(manifest["started_at"])),
                ended_at=UtcTimestamp.from_iso(str(manifest["ended_at"])),
                difference_report_id=(
                    parse_entity_id(str(difference)) if difference is not None else None
                ),
                failure=failure,
            )
        except Exception as error:
            raise ValidationEvidenceVerificationError(
                "validation run envelope is invalid"
            ) from error


def _read_file(path: Path) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        return path.read_bytes()
    except OSError as error:
        raise ValidationEvidenceVerificationError("required evidence file is missing") from error


def _schema_version_at(path: Path) -> str | None:
    """Read only the discriminator needed for version dispatch.

    Deep structural verification is still performed by the selected verifier;
    this helper must never be treated as evidence verification.
    """
    try:
        parsed = json.loads(_read_file(path))
    except (json.JSONDecodeError, ValidationEvidenceVerificationError):
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("manifest_schema_version")
    return value if isinstance(value, str) else None


def _verify_quality(
    artifacts_root: Path,
    *,
    dataset: DatasetKind,
    expected_rule: str,
    expected_schema: str,
    expected_content_sha: str,
) -> tuple[EntityId, str, bool]:
    directories = list((artifacts_root / "data-quality").glob("*"))
    if len(directories) != 1 or not directories[0].is_dir():
        raise ValueError("exactly one QualityRun artifact is required")
    directory = directories[0]
    manifest_bytes = _read_file(directory / "manifest.json")
    complete_bytes = _read_file(directory / "COMPLETE")
    manifest = json.loads(manifest_bytes)
    complete = json.loads(complete_bytes)
    if not isinstance(manifest, dict) or manifest_bytes != _canonical(manifest):
        raise ValueError("QualityRun manifest must be canonical")
    unsigned = dict(manifest)
    embedded = unsigned.pop("manifest_sha256", None)
    required = {
        "content_sha256",
        "ended_at",
        "manifest_sha256",
        "results",
        "rule_version",
        "run_id",
        "schema_version",
        "started_at",
        "status",
        "version_id",
    }
    if set(manifest) != required or (
        not isinstance(embedded, str)
        or compute_sha256(_canonical(unsigned)) != embedded
        or complete != {"manifest_sha256": embedded}
        or manifest.get("status") != "PASSED"
        or manifest.get("rule_version") != expected_rule
        or manifest.get("schema_version") != expected_schema
        or manifest.get("content_sha256") != expected_content_sha
    ):
        raise ValueError("QualityRun verdict or lineage mismatch")
    results = manifest.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("QualityRun requires actual rule results")
    parsed_results: list[QualityOutcomeResult] = []
    expected_files = {"manifest.json", "COMPLETE"}
    for result in results:
        if not isinstance(result, dict) or set(result) != {
            "affected_count",
            "row_evidence_relative_path",
            "rule_code",
            "status",
            "summary",
        }:
            raise ValueError("QualityResult fields do not match the contract")
        if result.get("status") != "PASSED":
            raise ValueError("QualityRun contains a non-passing result")
        path = result.get("row_evidence_relative_path")
        if path is not None:
            validate_managed_relative(str(path), field="quality row evidence")
            verify_quality_row_evidence(directory / str(path))
            expected_files.add(str(path))
        parsed_results.append(
            QualityOutcomeResult(
                rule_code=str(result["rule_code"]),
                status=RuleOutcomeStatus(str(result["status"])),
                affected_count=result["affected_count"],
                summary=str(result["summary"]),
                row_evidence_relative_path=str(path) if path is not None else None,
            )
        )
    outcome = QualityRunArtifact(
        run_id=parse_entity_id(str(manifest["run_id"])),
        version_id=parse_entity_id(str(manifest["version_id"])),
        rule_version=str(manifest["rule_version"]),
        schema_version=str(manifest["schema_version"]),
        content_sha256=str(manifest["content_sha256"]),
        started_at=UtcTimestamp.from_iso(str(manifest["started_at"])),
        ended_at=UtcTimestamp.from_iso(str(manifest["ended_at"])),
        status=QualityRunStatus(str(manifest["status"])),
        results=tuple(parsed_results),
        failure=None,
    )
    actual_files = {
        item.relative_to(directory).as_posix() for item in directory.rglob("*") if item.is_file()
    }
    if actual_files != expected_files:
        raise ValueError("QualityRun file set mismatch")
    catalog_ok = validate_quality_rule_catalog(
        dataset=dataset,
        rule_version=outcome.rule_version,
        result_rule_codes=tuple(result.rule_code for result in outcome.results),
    )
    return outcome.run_id, embedded, catalog_ok


def compute_evidence_inventory_sha256(manifest: Mapping[str, object]) -> str:
    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list):
        raise ValueError("manifest files must be an array")
    entries = [
        entry
        for entry in raw_entries
        if isinstance(entry, dict)
        and entry.get("relative_path") != "runner/result-attestation.json"
    ]
    if len(entries) != len(raw_entries) - 1:
        raise ValueError("exactly one result attestation is required")
    return compute_sha256(_canonical(entries))


def _verify_summary(directory: Path) -> tuple[dict[str, object], str]:
    manifest_bytes = _read_file(directory / "manifest.json")
    complete_bytes = _read_file(directory / "COMPLETE")
    manifest = json.loads(manifest_bytes)
    complete = json.loads(complete_bytes)
    if not isinstance(manifest, dict) or manifest_bytes != _canonical(manifest):
        raise ValidationEvidenceVerificationError("assessment manifest must be canonical")
    unsigned = dict(manifest)
    embedded = unsigned.pop("manifest_sha256", None)
    if (
        manifest.get("manifest_schema_version") != _ASSESSMENT_SCHEMA
        or not isinstance(embedded, str)
        or compute_sha256(_canonical(unsigned)) != embedded
        or complete != {"manifest_sha256": embedded}
        or {
            item.relative_to(directory).as_posix()
            for item in directory.rglob("*")
            if item.is_file()
        }
        != {"manifest.json", "COMPLETE"}
    ):
        raise ValidationEvidenceVerificationError("assessment manifest verification failed")
    required = {
        "assessment_id",
        "dataset",
        "gates",
        "manifest_schema_version",
        "manifest_sha256",
        "run_fingerprint",
        "runs",
        "sample_eligibility",
        "source",
    }
    if set(manifest) != required:
        raise ValidationEvidenceVerificationError("assessment fields do not match schema")
    return manifest, embedded


__all__ = [
    "ValidationEvidenceConflictError",
    "ValidationEvidenceVerificationError",
    "ValidationEvidenceVerifier",
    "ValidationEvidenceWriter",
    "compute_evidence_inventory_sha256",
]
