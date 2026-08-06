"""S1-07C v3 immutable evidence writer and deep verifier.

The v3 contract is intentionally separate from ``artifacts.py``'s accepted
v2 implementation.  It supports future trusted REAL *attempts* through a
protocol, but this module ships no REAL signer, runner, authorization path,
database write path, or runtime registration.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from quantking.common.ids import EntityId
from quantking.common.paths import is_reparse_point, resolve_under
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.validation_content import verify_canonical_evidence
from quantking.data.adapters.warehouse.manifest import (
    WAREHOUSE_COMPLETE_FILENAME,
    WAREHOUSE_MANIFEST_FILENAME,
    compute_sha256,
    path_safe_id,
    validate_managed_relative,
)
from quantking.data.adapters.warehouse.verifier import verify_raw_directory
from quantking.data.assessment.models import (
    AssessmentStatus,
    EvidenceKind,
    SourceCandidateAssessment,
)
from quantking.data.comparison.verifier import DifferenceReportVerifier
from quantking.data.models import DatasetKind
from quantking.data.validation.artifacts import (
    ValidationEvidenceConflictError,
    ValidationEvidenceVerificationError,
    _verify_quality,
)
from quantking.data.validation.contracts_v3 import (
    ASSESSMENT_SCHEMA_V3,
    RUN_SCHEMA_V3,
    AttemptOutcomeV3,
    PromotionProfileV3,
    ResultAttestationV3,
    SourceDatasetContractV3,
    SuccessEvidenceBindingV3,
    V3ResultAttestationSigner,
    ValidationAttemptIdentityV3,
    ValidationRequestManifestV3,
    canonical_json,
    compute_run_fingerprint_v3,
    sha256_json,
    verify_request_manifest_v3,
)
from quantking.data.validation.coverage import evaluate_fixed_sample_coverage
from quantking.data.validation.models import AssessmentGates, ValidationFailure, ValidationVersions
from quantking.data.validation.samples import FixedSampleSpec


@dataclass(frozen=True, slots=True)
class ValidationRunEnvelopeV3:
    """Unverified v3 envelope.  Gates and credential hashes are not writable facts."""

    identity: ValidationAttemptIdentityV3
    source: str
    dataset: DatasetKind
    versions: ValidationVersions
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
    outcome: AttemptOutcomeV3
    failure: ValidationFailure | None
    started_at: UtcTimestamp
    ended_at: UtcTimestamp

    def __post_init__(self) -> None:
        # Construct a signature-free attestation only to reuse its strict
        # identity/context/outcome validation.  No credential values enter the
        # envelope and no signer is involved in this check.
        ResultAttestationV3(
            signer_key_id="envelope-validator",
            identity=self.identity,
            source=self.source,
            dataset=self.dataset,
            request_sha256=self.request_sha256,
            run_fingerprint=self.run_fingerprint,
            selected_case_ids=self.selected_case_ids,
            sample_spec_version=self.sample_spec_version,
            sample_spec_sha256=self.sample_spec_sha256,
            source_dataset_contract_version=self.source_dataset_contract_version,
            source_dataset_contract_sha256=self.source_dataset_contract_sha256,
            promotion_profile_version=self.promotion_profile_version,
            promotion_profile_sha256=self.promotion_profile_sha256,
            reference_bundle_id=self.reference_bundle_id,
            reference_bundle_manifest_sha256=self.reference_bundle_manifest_sha256,
            versions=self.versions.to_dict(),
            outcome=self.outcome,
            failure=self.failure,
            success_evidence=(
                _placeholder_success_evidence()
                if self.outcome is AttemptOutcomeV3.SUCCEEDED
                else None
            ),
            started_at=self.started_at,
            ended_at=self.ended_at,
            signature="0" * 64,
        )

    def to_payload(self) -> dict[str, object]:
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
            "run_fingerprint": self.run_fingerprint,
            "sample_spec_sha256": self.sample_spec_sha256,
            "sample_spec_version": self.sample_spec_version,
            "selected_case_ids": list(self.selected_case_ids),
            "source": self.source,
            "source_dataset_contract_sha256": self.source_dataset_contract_sha256,
            "source_dataset_contract_version": self.source_dataset_contract_version,
            "started_at": self.started_at.to_iso(),
            "ended_at": self.ended_at.to_iso(),
            "versions": self.versions.to_dict(),
        }


def _placeholder_success_evidence() -> SuccessEvidenceBindingV3:
    return SuccessEvidenceBindingV3(
        raw_manifest_sha256="0" * 64,
        canonical_content_sha256="0" * 64,
        quality_manifest_sha256="0" * 64,
        quality_run_id=EntityId("data_quality", UUID("00000000-0000-4000-8000-000000000001")),
        difference_manifest_sha256="0" * 64,
        difference_report_id=EntityId("data_diff", UUID("00000000-0000-4000-8000-000000000002")),
        semantic_evidence_sha256="0" * 64,
        schema_unit_evidence_sha256="0" * 64,
        evidence_inventory_sha256="0" * 64,
    )


@dataclass(frozen=True, slots=True)
class V3EvidenceTrustContext:
    """Injected controlled profile; no public default can promote a v3 run."""

    sample: FixedSampleSpec
    source_dataset_contract: SourceDatasetContractV3
    promotion_profile: PromotionProfileV3

    def __post_init__(self) -> None:
        if self.source_dataset_contract.source != self.promotion_profile.source or (
            self.source_dataset_contract.dataset is not self.promotion_profile.dataset
        ):
            raise ValueError("v3 contract/profile must bind one source x dataset")
        if self.promotion_profile.promotion_config_version == "":
            raise ValueError("v3 promotion config version is required")
        case_ids = {case.case_id for case in self.sample.cases}
        if not set(self.promotion_profile.required_case_ids).issubset(case_ids):
            raise ValueError("v3 promotion profile is not bound to the controlled sample")


@dataclass(frozen=True, slots=True)
class VerifiedValidationRunV3:
    envelope: ValidationRunEnvelopeV3
    evidence_manifest_sha256: str
    evidence_kind: EvidenceKind
    gates: AssessmentGates
    raw_manifest_sha256: str | None
    canonical_content_sha256: str | None
    quality_manifest_sha256: str | None
    quality_run_id: EntityId | None
    difference_manifest_sha256: str | None
    difference_report_id: EntityId | None
    verification_reason: str | None

    @property
    def failure(self) -> ValidationFailure | None:
        return self.envelope.failure

    @property
    def outcome(self) -> AttemptOutcomeV3:
        return self.envelope.outcome

    @property
    def identity(self) -> ValidationAttemptIdentityV3:
        return self.envelope.identity


@dataclass(frozen=True, slots=True)
class VerifiedAssessmentEvidenceV3:
    assessment: SourceCandidateAssessment
    verified_runs: tuple[VerifiedValidationRunV3, ...]
    evidence_manifest_sha256: str
    evidence_relative_path: str
    common_run_fingerprint: str | None
    context: V3EvidenceTrustContext


class ValidationEvidenceWriterV3:
    """Immutable v3 writer.  It rejects success material in failed attempts."""

    def __init__(self, *, validation_root: Path) -> None:
        self._root = _validate_root(validation_root)

    def write(
        self,
        *,
        envelope: ValidationRunEnvelopeV3,
        files: Mapping[str, tuple[bytes, int]],
    ) -> Path:
        if not isinstance(envelope, ValidationRunEnvelopeV3):
            raise TypeError("v3 writer requires a ValidationRunEnvelopeV3")
        normalized = _normalize_files(files)
        _validate_attempt_file_shape(envelope=envelope, files={item[0] for item in normalized})
        manifest_files = [
            {
                "relative_path": relative,
                "row_count": row_count,
                "sha256": compute_sha256(payload),
                "size": len(payload),
            }
            for relative, payload, row_count in normalized
        ]
        unsigned = {
            "files": manifest_files,
            "manifest_schema_version": RUN_SCHEMA_V3,
            **envelope.to_payload(),
        }
        manifest_sha = compute_sha256(canonical_json(unsigned))
        manifest_bytes = canonical_json({**unsigned, "manifest_sha256": manifest_sha})
        complete_bytes = canonical_json({"manifest_sha256": manifest_sha})
        directory = self._directory(envelope)
        if directory.exists():
            if (directory / WAREHOUSE_MANIFEST_FILENAME).read_bytes() == manifest_bytes and (
                directory / WAREHOUSE_COMPLETE_FILENAME
            ).read_bytes() == complete_bytes:
                return directory
            raise ValidationEvidenceConflictError(
                "v3 validation run ID already exists with different content"
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
                        canonical_json({"category": "EVIDENCE_WRITE_FAILURE", "retryable": False})
                    )
            raise
        return directory

    def _directory(self, envelope: ValidationRunEnvelopeV3) -> Path:
        candidate = (
            self._root
            / "source-assessments-v3"
            / envelope.source.lower()
            / envelope.dataset.value
            / path_safe_id(str(envelope.identity.assessment_id))
            / path_safe_id(str(envelope.identity.run_id))
        )
        try:
            candidate.resolve(strict=False).relative_to(self._root.resolve(strict=False))
        except ValueError as error:
            raise ValueError("v3 validation evidence path escapes the managed root") from error
        return candidate

    def finalize_assessment(
        self,
        *,
        assessment_directory: Path,
        verifier: ValidationEvidenceVerifierV3,
    ) -> VerifiedAssessmentEvidenceV3:
        runs = tuple(
            verifier.verify_run(directory=child)
            for child in sorted(assessment_directory.iterdir(), key=lambda item: item.name)
            if child.is_dir()
            and not child.name.startswith("_assessment")
            and not child.name.endswith(".incomplete")
        )
        if not runs:
            raise ValueError("v3 assessment requires at least one completed validation run")
        summary = _assessment_summary_payload(runs=runs, context=verifier.context)
        manifest_sha = compute_sha256(canonical_json(summary))
        manifest_bytes = canonical_json({**summary, "manifest_sha256": manifest_sha})
        complete_bytes = canonical_json({"manifest_sha256": manifest_sha})
        directory = assessment_directory / "_assessment-v3"
        if directory.exists():
            if (directory / "manifest.json").read_bytes() != manifest_bytes:
                raise ValidationEvidenceConflictError(
                    "v3 assessment is already finalized with different evidence"
                )
            return verifier.verify_assessment(assessment_directory=assessment_directory)
        incomplete = assessment_directory / "_assessment-v3.incomplete"
        incomplete.mkdir(parents=False, exist_ok=False)
        (incomplete / "manifest.json").write_bytes(manifest_bytes)
        (incomplete / "COMPLETE").write_bytes(complete_bytes)
        os.replace(incomplete, directory)
        return verifier.verify_assessment(assessment_directory=assessment_directory)


class ValidationEvidenceVerifierV3:
    """Deep v3 verifier.  Context and signer are injected, never artifact supplied."""

    def __init__(
        self,
        *,
        validation_root: Path,
        context: V3EvidenceTrustContext,
        runner_attestation_verifier: V3ResultAttestationSigner,
    ) -> None:
        self._root = _validate_root(validation_root)
        self.context = context
        self._runner_trust = runner_attestation_verifier

    def verify(self, *, directory: Path) -> VerifiedAssessmentEvidenceV3:
        """Verify one v3 run as a deliberately BLOCKED one-run assessment.

        This is the version-dispatch counterpart to the accepted v2 ``verify``
        entry point.  Production promotion still requires the signed v3
        assessment summary and the profile's complete independent run set.
        """
        run = self.verify_run(directory=directory)
        manifest = json.loads((directory / "manifest.json").read_bytes())
        evidence_sha = str(manifest["manifest_sha256"])
        assessment = _assess_v3(
            runs=(run,),
            context=self.context,
            evidence_relative_path=directory.relative_to(self._root).as_posix(),
            evidence_sha256=evidence_sha,
            created_at=run.envelope.ended_at,
        )
        return VerifiedAssessmentEvidenceV3(
            assessment=assessment,
            verified_runs=(run,),
            evidence_manifest_sha256=evidence_sha,
            evidence_relative_path=directory.relative_to(self._root).as_posix(),
            common_run_fingerprint=run.envelope.run_fingerprint,
            context=self.context,
        )

    def verify_run(self, *, directory: Path) -> VerifiedValidationRunV3:
        manifest, manifest_sha, files = _verify_outer_v3(root=self._root, directory=directory)
        envelope = _parse_envelope_v3(manifest)
        try:
            request = verify_request_manifest_v3(
                files["request/manifest.json"],
                source=envelope.source,
                dataset=envelope.dataset,
                sample=self.context.sample,
                source_dataset_contract=self.context.source_dataset_contract,
                promotion_profile=self.context.promotion_profile,
            )
            _verify_envelope_request_binding(envelope=envelope, request=request)
            attestation = ResultAttestationV3.from_bytes(files["runner/result-attestation.v2.json"])
            _verify_attestation_binding(
                attestation=attestation,
                envelope=envelope,
                request=request,
                manifest=manifest,
                signer=self._runner_trust,
            )
            if envelope.outcome is AttemptOutcomeV3.FAILED:
                _verify_failed_attempt(envelope=envelope, files=files)
                return VerifiedValidationRunV3(
                    envelope=envelope,
                    evidence_manifest_sha256=manifest_sha,
                    evidence_kind=self._runner_trust.evidence_kind,
                    gates=AssessmentGates.none_passed(),
                    raw_manifest_sha256=None,
                    canonical_content_sha256=None,
                    quality_manifest_sha256=None,
                    quality_run_id=None,
                    difference_manifest_sha256=None,
                    difference_report_id=None,
                    verification_reason=envelope.failure.category
                    if envelope.failure
                    else "FAILED_ATTEMPT",
                )
            return self._verify_successful_attempt(
                directory=directory,
                envelope=envelope,
                manifest_sha=manifest_sha,
                files=files,
                attestation=attestation,
            )
        except ValidationEvidenceVerificationError:
            raise
        except Exception as error:
            raise ValidationEvidenceVerificationError(
                "v3 validation evidence verification failed"
            ) from error

    def _verify_successful_attempt(
        self,
        *,
        directory: Path,
        envelope: ValidationRunEnvelopeV3,
        manifest_sha: str,
        files: Mapping[str, bytes],
        attestation: ResultAttestationV3,
    ) -> VerifiedValidationRunV3:
        if attestation.success_evidence is None:
            raise ValidationEvidenceVerificationError(
                "successful v3 attempt lacks success evidence"
            )
        evidence = attestation.success_evidence
        raw_manifests = [
            path for path in directory.glob("raw-root/raw/*/*/*/manifest.json") if path.is_file()
        ]
        if len(raw_manifests) != 1:
            raise ValidationEvidenceVerificationError(
                "v3 success requires exactly one Raw manifest"
            )
        raw = verify_raw_directory(raw_manifests[0].parent, data_root=directory / "raw-root")
        if (
            raw.manifest["source"] != envelope.source
            or raw.manifest["dataset"] != envelope.dataset.value
            or raw.manifest["adapter_version"] != envelope.versions.adapter_version
            or raw.manifest["code_version"] != envelope.versions.code_version
            or raw.manifest["request_params"]
            != {"request_sha256": envelope.request_sha256, "run_id": str(envelope.identity.run_id)}
            or raw.manifest_sha != evidence.raw_manifest_sha256
        ):
            raise ValidationEvidenceVerificationError("v3 Raw lineage does not bind the attempt")
        canonical = verify_canonical_evidence(
            path=directory / "canonical" / "content.parquet", dataset=envelope.dataset
        )
        if canonical.content_sha256 != evidence.canonical_content_sha256:
            raise ValidationEvidenceVerificationError(
                "v3 Canonical credential does not bind content"
            )
        quality_id, quality_sha, quality_ok = _verify_quality(
            directory / "quality-root",
            dataset=envelope.dataset,
            expected_rule=envelope.versions.rule_version,
            expected_schema=envelope.versions.schema_version,
            expected_content_sha=canonical.content_sha256,
        )
        if quality_id != evidence.quality_run_id or quality_sha != evidence.quality_manifest_sha256:
            raise ValidationEvidenceVerificationError("v3 Quality credential does not bind content")
        report = DifferenceReportVerifier(artifacts_root=directory / "difference-root").verify(
            report_id=evidence.difference_report_id
        )
        expected_difference_sample_sha = sha256_json(list(envelope.selected_case_ids))
        difference_ok = (
            report.manifest_sha256 == evidence.difference_manifest_sha256
            and report.dataset == envelope.dataset.value
            and report.schema_version == envelope.versions.schema_version
            and report.threshold_rule_version == envelope.versions.threshold_version
            and report.sample_spec_sha256 == expected_difference_sample_sha
            and canonical.content_sha256
            in {report.primary_content_sha256, report.fallback_content_sha256}
        )
        semantic_ok = _verify_semantic_evidence(
            payload=files["semantic/result.json"],
            envelope=envelope,
            canonical_content_sha256=canonical.content_sha256,
            expected_sha=evidence.semantic_evidence_sha256,
            context=self.context,
        )
        schema_unit_ok = _verify_schema_unit_evidence(
            payload=files["schema-unit/result.json"],
            envelope=envelope,
            canonical_content_sha256=canonical.content_sha256,
            expected_sha=evidence.schema_unit_evidence_sha256,
            context=self.context,
        )
        calendar_rows: list[dict[str, object]] = []
        if envelope.dataset is not DatasetKind.EXCHANGE_CALENDAR:
            reference_path = directory / "reference" / "exchange_calendar.parquet"
            if reference_path.is_file():
                calendar_rows = list(
                    verify_canonical_evidence(
                        path=reference_path, dataset=DatasetKind.EXCHANGE_CALENDAR
                    ).rows
                )
        coverage = evaluate_fixed_sample_coverage(
            dataset=envelope.dataset,
            rows=list(canonical.rows),
            cases=list(
                ValidationRequestManifestV3.for_controlled_sample(
                    identity=envelope.identity,
                    source=envelope.source,
                    dataset=envelope.dataset,
                    sample=self.context.sample,
                    selected_case_ids=envelope.selected_case_ids,
                    source_dataset_contract=self.context.source_dataset_contract,
                    promotion_profile=self.context.promotion_profile,
                ).selected_cases
            ),
            calendar_rows=calendar_rows,
        )
        return VerifiedValidationRunV3(
            envelope=envelope,
            evidence_manifest_sha256=manifest_sha,
            evidence_kind=self._runner_trust.evidence_kind,
            gates=AssessmentGates(
                semantic_contract_verified=semantic_ok,
                schema_unit_verified=schema_unit_ok,
                coverage_adequate=coverage,
                quality_passed=quality_ok,
                fixed_sample_differences_adequate=difference_ok,
            ),
            raw_manifest_sha256=raw.manifest_sha,
            canonical_content_sha256=canonical.content_sha256,
            quality_manifest_sha256=quality_sha,
            quality_run_id=quality_id,
            difference_manifest_sha256=report.manifest_sha256,
            difference_report_id=report.report_id,
            verification_reason=(
                None
                if all((semantic_ok, schema_unit_ok, coverage, quality_ok, difference_ok))
                else "SUCCESSFUL_ATTEMPT_WITH_BLOCKING_GATE"
            ),
        )

    def verify_assessment(self, *, assessment_directory: Path) -> VerifiedAssessmentEvidenceV3:
        summary_dir = assessment_directory / "_assessment-v3"
        manifest, manifest_sha = _verify_summary_v3(summary_dir)
        declared = manifest["runs"]
        if not isinstance(declared, list) or not declared:
            raise ValidationEvidenceVerificationError("v3 assessment runs are invalid")
        runs: list[VerifiedValidationRunV3] = []
        for entry in declared:
            if not isinstance(entry, dict) or set(entry) != {
                "directory",
                "identity",
                "manifest_sha256",
                "run_fingerprint",
            }:
                raise ValidationEvidenceVerificationError("v3 assessment run entry is invalid")
            directory = assessment_directory / str(entry["directory"])
            run_manifest = json.loads((directory / "manifest.json").read_bytes())
            run = self.verify_run(directory=directory)
            if (
                run_manifest.get("manifest_sha256") != entry["manifest_sha256"]
                or run.identity.to_dict() != entry["identity"]
                or run.envelope.run_fingerprint != entry["run_fingerprint"]
            ):
                raise ValidationEvidenceVerificationError("v3 assessment run lineage mismatch")
            runs.append(run)
        run_tuple = tuple(runs)
        expected = _assessment_summary_payload(runs=run_tuple, context=self.context)
        unsigned = dict(manifest)
        unsigned.pop("manifest_sha256", None)
        if unsigned != expected:
            raise ValidationEvidenceVerificationError(
                "v3 assessment summary does not match deep evidence"
            )
        assessment = _assess_v3(
            runs=run_tuple,
            context=self.context,
            evidence_relative_path=summary_dir.relative_to(self._root).as_posix(),
            evidence_sha256=manifest_sha,
            created_at=max(
                (run.envelope.ended_at for run in run_tuple), key=lambda item: item.value
            ),
        )
        return VerifiedAssessmentEvidenceV3(
            assessment=assessment,
            verified_runs=run_tuple,
            evidence_manifest_sha256=manifest_sha,
            evidence_relative_path=summary_dir.relative_to(self._root).as_posix(),
            common_run_fingerprint=_common_fingerprint_v3(run_tuple),
            context=self.context,
        )


def _validate_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("validation_root must be an absolute Path")
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        if current.exists() and is_reparse_point(current):
            raise ValueError("validation_root must not contain a symlink or junction")
    return root


def _normalize_files(files: Mapping[str, tuple[bytes, int]]) -> list[tuple[str, bytes, int]]:
    normalized: list[tuple[str, bytes, int]] = []
    for relative, item in files.items():
        validate_managed_relative(relative, field="v3 validation evidence file")
        if relative in {WAREHOUSE_MANIFEST_FILENAME, WAREHOUSE_COMPLETE_FILENAME}:
            raise ValueError("reserved v3 validation evidence file name")
        payload, row_count = item
        if not isinstance(payload, bytes):
            raise TypeError("v3 validation evidence payload must be bytes")
        if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
            raise ValueError("v3 validation evidence row_count must be non-negative")
        normalized.append((relative, payload, row_count))
    normalized.sort()
    if not normalized:
        raise ValueError("v3 validation evidence files must not be empty")
    return normalized


def _validate_attempt_file_shape(*, envelope: ValidationRunEnvelopeV3, files: set[str]) -> None:
    base = {"request/manifest.json", "runner/result-attestation.v2.json"}
    if envelope.outcome is AttemptOutcomeV3.FAILED:
        expected = {*base, "failure/attempt.json"}
        forbidden = (
            "raw-root/",
            "canonical/",
            "quality-root/",
            "difference-root/",
            "semantic/",
            "schema-unit/",
        )
        if files != expected or any(path.startswith(forbidden) for path in files):
            raise ValueError("failed v3 attempts must not carry successful credentials")
        return
    required_prefixes = (
        "raw-root/raw/",
        "canonical/content.parquet",
        "quality-root/data-quality/",
        "difference-root/data-diff/",
        "semantic/result.json",
        "schema-unit/result.json",
    )
    if not base.issubset(files) or not all(
        any(path.startswith(prefix) for path in files) for prefix in required_prefixes
    ):
        raise ValueError("successful v3 attempts require the complete credential families")


def _verify_outer_v3(
    *, root: Path, directory: Path
) -> tuple[dict[str, object], str, dict[str, bytes]]:
    try:
        resolved = resolve_under(root, directory.relative_to(root))
    except Exception as error:
        raise ValidationEvidenceVerificationError(
            "v3 validation directory is outside the managed root"
        ) from error
    if not resolved.is_dir() or is_reparse_point(resolved):
        raise ValidationEvidenceVerificationError("v3 validation directory is unsafe")
    manifest_bytes = _read_file(directory / "manifest.json")
    complete_bytes = _read_file(directory / "COMPLETE")
    try:
        manifest = json.loads(manifest_bytes)
        complete = json.loads(complete_bytes)
    except json.JSONDecodeError as error:
        raise ValidationEvidenceVerificationError("v3 manifest or COMPLETE is invalid") from error
    if not isinstance(manifest, dict) or manifest_bytes != canonical_json(manifest):
        raise ValidationEvidenceVerificationError("v3 manifest must be canonical JSON")
    unsigned = dict(manifest)
    embedded = unsigned.pop("manifest_sha256", None)
    if (
        manifest.get("manifest_schema_version") != RUN_SCHEMA_V3
        or not isinstance(embedded, str)
        or compute_sha256(canonical_json(unsigned)) != embedded
        or complete != {"manifest_sha256": embedded}
    ):
        raise ValidationEvidenceVerificationError("v3 manifest self-SHA or COMPLETE mismatch")
    required = {
        "attempt_outcome",
        "dataset",
        "ended_at",
        "failure",
        "files",
        "identity",
        "manifest_schema_version",
        "manifest_sha256",
        "promotion_profile_sha256",
        "promotion_profile_version",
        "reference_bundle_id",
        "reference_bundle_manifest_sha256",
        "request_sha256",
        "run_fingerprint",
        "sample_spec_sha256",
        "sample_spec_version",
        "selected_case_ids",
        "source",
        "source_dataset_contract_sha256",
        "source_dataset_contract_version",
        "started_at",
        "versions",
    }
    if set(manifest) != required or not isinstance(manifest["files"], list):
        raise ValidationEvidenceVerificationError("v3 manifest fields do not match the schema")
    files: dict[str, bytes] = {}
    expected_files = {"manifest.json", "COMPLETE"}
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {
            "relative_path",
            "row_count",
            "sha256",
            "size",
        }:
            raise ValidationEvidenceVerificationError("invalid v3 manifest file entry")
        relative = str(entry["relative_path"])
        try:
            validate_managed_relative(relative, field="v3 validation evidence file")
            payload = _read_file(resolve_under(directory, relative))
        except Exception as error:
            raise ValidationEvidenceVerificationError("unsafe v3 evidence file") from error
        if (
            relative in files
            or len(payload) != entry["size"]
            or compute_sha256(payload) != entry["sha256"]
            or not isinstance(entry["row_count"], int)
            or isinstance(entry["row_count"], bool)
            or entry["row_count"] < 0
        ):
            raise ValidationEvidenceVerificationError("v3 validation evidence file mismatch")
        files[relative] = payload
        expected_files.add(relative)
    actual_files = {
        item.relative_to(directory).as_posix() for item in directory.rglob("*") if item.is_file()
    }
    if actual_files != expected_files:
        raise ValidationEvidenceVerificationError("v3 validation evidence file set mismatch")
    return manifest, embedded, files


def _parse_envelope_v3(manifest: Mapping[str, object]) -> ValidationRunEnvelopeV3:
    try:
        raw_failure = manifest["failure"]
        raw_identity = manifest["identity"]
        raw_cases = manifest["selected_case_ids"]
        if not isinstance(raw_identity, dict) or not isinstance(raw_cases, list):
            raise ValueError
        if raw_failure is not None and not isinstance(raw_failure, Mapping):
            raise ValueError
        failure = (
            None
            if raw_failure is None
            else ValidationFailure(
                category=str(raw_failure["category"]), retryable=raw_failure["retryable"]
            )
        )
        return ValidationRunEnvelopeV3(
            identity=ValidationAttemptIdentityV3.from_dict(raw_identity),
            source=str(manifest["source"]),
            dataset=DatasetKind(str(manifest["dataset"])),
            versions=ValidationVersions(**manifest["versions"]),  # type: ignore[arg-type]
            request_sha256=str(manifest["request_sha256"]),
            run_fingerprint=str(manifest["run_fingerprint"]),
            selected_case_ids=tuple(str(item) for item in raw_cases),
            sample_spec_version=str(manifest["sample_spec_version"]),
            sample_spec_sha256=str(manifest["sample_spec_sha256"]),
            source_dataset_contract_version=str(manifest["source_dataset_contract_version"]),
            source_dataset_contract_sha256=str(manifest["source_dataset_contract_sha256"]),
            promotion_profile_version=str(manifest["promotion_profile_version"]),
            promotion_profile_sha256=str(manifest["promotion_profile_sha256"]),
            reference_bundle_id=str(manifest["reference_bundle_id"]),
            reference_bundle_manifest_sha256=str(manifest["reference_bundle_manifest_sha256"]),
            outcome=AttemptOutcomeV3(str(manifest["attempt_outcome"])),
            failure=failure,
            started_at=UtcTimestamp.from_iso(str(manifest["started_at"])),
            ended_at=UtcTimestamp.from_iso(str(manifest["ended_at"])),
        )
    except Exception as error:
        raise ValidationEvidenceVerificationError("v3 run envelope is invalid") from error


def _verify_envelope_request_binding(
    *, envelope: ValidationRunEnvelopeV3, request: ValidationRequestManifestV3
) -> None:
    expected = compute_run_fingerprint_v3(request=request, versions=envelope.versions)
    if (
        envelope.identity != request.identity
        or envelope.source != request.source
        or envelope.dataset is not request.dataset
        or envelope.request_sha256 != request.request_sha256
        or envelope.run_fingerprint != expected
        or envelope.selected_case_ids != request.selected_case_ids
        or envelope.sample_spec_version != request.sample_spec_version
        or envelope.sample_spec_sha256 != request.sample_spec_sha256
        or envelope.source_dataset_contract_version
        != request.source_dataset_contract.contract_version
        or envelope.source_dataset_contract_sha256 != request.source_dataset_contract.sha256
        or envelope.promotion_profile_version != request.promotion_profile.promotion_profile_version
        or envelope.promotion_profile_sha256 != request.promotion_profile.sha256
        or envelope.reference_bundle_id != request.promotion_profile.reference_bundle_id
        or envelope.reference_bundle_manifest_sha256
        != request.promotion_profile.reference_bundle_manifest_sha256
    ):
        raise ValidationEvidenceVerificationError("v3 envelope does not bind the request context")


def _verify_attestation_binding(
    *,
    attestation: ResultAttestationV3,
    envelope: ValidationRunEnvelopeV3,
    request: ValidationRequestManifestV3,
    manifest: Mapping[str, object],
    signer: V3ResultAttestationSigner,
) -> None:
    if attestation.signer_key_id != signer.key_id or not signer.verify(attestation):
        raise ValidationEvidenceVerificationError("v3 result attestation is untrusted")
    if (
        attestation.identity != envelope.identity
        or attestation.source != envelope.source
        or attestation.dataset is not envelope.dataset
        or attestation.request_sha256 != envelope.request_sha256
        or attestation.run_fingerprint != envelope.run_fingerprint
        or attestation.selected_case_ids != envelope.selected_case_ids
        or attestation.sample_spec_version != envelope.sample_spec_version
        or attestation.sample_spec_sha256 != envelope.sample_spec_sha256
        or attestation.source_dataset_contract_version != envelope.source_dataset_contract_version
        or attestation.source_dataset_contract_sha256 != envelope.source_dataset_contract_sha256
        or attestation.promotion_profile_version != envelope.promotion_profile_version
        or attestation.promotion_profile_sha256 != envelope.promotion_profile_sha256
        or attestation.reference_bundle_id != envelope.reference_bundle_id
        or attestation.reference_bundle_manifest_sha256 != envelope.reference_bundle_manifest_sha256
        or dict(attestation.versions) != envelope.versions.to_dict()
        or attestation.outcome is not envelope.outcome
        or attestation.failure != envelope.failure
        or attestation.started_at != envelope.started_at
        or attestation.ended_at != envelope.ended_at
        or request.identity != attestation.identity
    ):
        raise ValidationEvidenceVerificationError("v3 result attestation is unbound")
    if attestation.outcome is AttemptOutcomeV3.SUCCEEDED:
        assert attestation.success_evidence is not None
        if attestation.success_evidence.evidence_inventory_sha256 != _inventory_sha(manifest):
            raise ValidationEvidenceVerificationError("v3 result attestation inventory mismatch")


def _verify_failed_attempt(
    *, envelope: ValidationRunEnvelopeV3, files: Mapping[str, bytes]
) -> None:
    _validate_attempt_file_shape(envelope=envelope, files=set(files))
    try:
        payload = json.loads(files["failure/attempt.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationEvidenceVerificationError(
            "v3 failure attempt evidence is invalid"
        ) from error
    expected = {
        "attempt_outcome": "FAILED",
        "failure": {
            "category": envelope.failure.category if envelope.failure else None,
            "retryable": envelope.failure.retryable if envelope.failure else None,
        },
        "identity": envelope.identity.to_dict(),
        "request_sha256": envelope.request_sha256,
    }
    if payload != expected or files["failure/attempt.json"] != canonical_json(payload):
        raise ValidationEvidenceVerificationError("v3 failure attempt evidence is unbound")


def _verify_semantic_evidence(
    *,
    payload: bytes,
    envelope: ValidationRunEnvelopeV3,
    canonical_content_sha256: str,
    expected_sha: str,
    context: V3EvidenceTrustContext,
) -> bool:
    if compute_sha256(payload) != expected_sha:
        raise ValidationEvidenceVerificationError("v3 semantic credential SHA mismatch")
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationEvidenceVerificationError("v3 semantic credential is invalid") from error
    required = {
        "canonical_content_sha256",
        "dataset",
        "semantic_rule_version",
        "selected_case_ids",
        "semantic_evidence_schema_version",
        "source",
        "source_dataset_contract_sha256",
        "violations",
    }
    if (
        not isinstance(parsed, dict)
        or payload != canonical_json(parsed)
        or set(parsed) != required
        or parsed["semantic_evidence_schema_version"] != "source-validation-semantic-evidence.v1"
        or parsed["source"] != envelope.source
        or parsed["dataset"] != envelope.dataset.value
        or parsed["semantic_rule_version"] != context.source_dataset_contract.semantic_rule_version
        or parsed["source_dataset_contract_sha256"] != context.source_dataset_contract.sha256
        or parsed["canonical_content_sha256"] != canonical_content_sha256
        or parsed["selected_case_ids"] != list(envelope.selected_case_ids)
        or not isinstance(parsed["violations"], list)
        or any(not isinstance(item, str) or not item for item in parsed["violations"])
    ):
        raise ValidationEvidenceVerificationError("v3 semantic credential is unbound")
    return not parsed["violations"]


def _expected_units(
    context: V3EvidenceTrustContext, selected_case_ids: tuple[str, ...]
) -> dict[str, dict[str, str]]:
    by_id = {case.case_id: case for case in context.sample.cases}
    return {
        case_id: dict(by_id[case_id].expected_units)
        for case_id in selected_case_ids
        if case_id in by_id
    }


def _verify_schema_unit_evidence(
    *,
    payload: bytes,
    envelope: ValidationRunEnvelopeV3,
    canonical_content_sha256: str,
    expected_sha: str,
    context: V3EvidenceTrustContext,
) -> bool:
    if compute_sha256(payload) != expected_sha:
        raise ValidationEvidenceVerificationError("v3 schema/unit credential SHA mismatch")
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationEvidenceVerificationError("v3 schema/unit credential is invalid") from error
    required = {
        "canonical_content_sha256",
        "dataset",
        "observed_units_by_case",
        "schema_unit_evidence_schema_version",
        "schema_version",
        "selected_case_ids",
        "source",
        "source_dataset_contract_sha256",
        "unit_deviations",
        "unit_rule_version",
    }
    if (
        not isinstance(parsed, dict)
        or payload != canonical_json(parsed)
        or set(parsed) != required
        or parsed["schema_unit_evidence_schema_version"]
        != "source-validation-schema-unit-evidence.v1"
        or parsed["source"] != envelope.source
        or parsed["dataset"] != envelope.dataset.value
        or parsed["source_dataset_contract_sha256"] != context.source_dataset_contract.sha256
        or parsed["schema_version"] != context.source_dataset_contract.expected_schema_version
        or parsed["unit_rule_version"] != context.source_dataset_contract.unit_rule_version
        or parsed["canonical_content_sha256"] != canonical_content_sha256
        or parsed["selected_case_ids"] != list(envelope.selected_case_ids)
        or parsed["observed_units_by_case"] != _expected_units(context, envelope.selected_case_ids)
        or not isinstance(parsed["unit_deviations"], list)
        or any(not isinstance(item, str) or not item for item in parsed["unit_deviations"])
    ):
        raise ValidationEvidenceVerificationError("v3 schema/unit credential is unbound")
    return not parsed["unit_deviations"]


def _inventory_sha(manifest: Mapping[str, object]) -> str:
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValidationEvidenceVerificationError("v3 manifest files are invalid")
    inventory = [
        item
        for item in entries
        if isinstance(item, dict)
        and item.get("relative_path") != "runner/result-attestation.v2.json"
    ]
    if len(inventory) != len(entries) - 1:
        raise ValidationEvidenceVerificationError("v3 attestation file inventory is invalid")
    return compute_sha256(canonical_json(inventory))


def _assessment_summary_payload(
    *, runs: tuple[VerifiedValidationRunV3, ...], context: V3EvidenceTrustContext
) -> dict[str, object]:
    if not runs:
        raise ValueError("v3 assessment requires runs")
    first = runs[0].envelope
    if any(
        run.envelope.identity.assessment_id != first.identity.assessment_id
        or run.envelope.source != first.source
        or run.envelope.dataset is not first.dataset
        or run.envelope.selected_case_ids != first.selected_case_ids
        or run.envelope.source_dataset_contract_version != first.source_dataset_contract_version
        or run.envelope.source_dataset_contract_sha256 != first.source_dataset_contract_sha256
        or run.envelope.promotion_profile_version != first.promotion_profile_version
        or run.envelope.promotion_profile_sha256 != first.promotion_profile_sha256
        or run.envelope.reference_bundle_id != first.reference_bundle_id
        or run.envelope.reference_bundle_manifest_sha256 != first.reference_bundle_manifest_sha256
        or run.envelope.run_fingerprint != first.run_fingerprint
        for run in runs
    ):
        raise ValueError("v3 assessment runs have mixed frozen evidence context")
    gates = _aggregate_gates_v3(runs)
    return {
        "assessment_id": str(first.identity.assessment_id),
        "dataset": first.dataset.value,
        "gates": gates.to_dict(),
        "manifest_schema_version": ASSESSMENT_SCHEMA_V3,
        "promotion_profile_sha256": context.promotion_profile.sha256,
        "promotion_profile_version": context.promotion_profile.promotion_profile_version,
        "reference_bundle_id": context.promotion_profile.reference_bundle_id,
        "reference_bundle_manifest_sha256": (
            context.promotion_profile.reference_bundle_manifest_sha256
        ),
        "run_fingerprint": _common_fingerprint_v3(runs),
        "runs": [
            {
                "directory": path_safe_id(str(run.identity.run_id)),
                "identity": run.identity.to_dict(),
                "manifest_sha256": _run_manifest_sha(run),
                "run_fingerprint": run.envelope.run_fingerprint,
            }
            for run in runs
        ],
        "sample_spec_sha256": context.sample.sample_spec_sha256,
        "sample_spec_version": context.sample.sample_spec_version,
        "selected_case_ids": list(first.selected_case_ids),
        "source": first.source,
        "source_dataset_contract_sha256": context.source_dataset_contract.sha256,
        "source_dataset_contract_version": context.source_dataset_contract.contract_version,
    }


def _run_manifest_sha(run: VerifiedValidationRunV3) -> str:
    return run.evidence_manifest_sha256


def _aggregate_gates_v3(runs: tuple[VerifiedValidationRunV3, ...]) -> AssessmentGates:
    return AssessmentGates(
        semantic_contract_verified=all(run.gates.semantic_contract_verified for run in runs),
        schema_unit_verified=all(run.gates.schema_unit_verified for run in runs),
        coverage_adequate=all(run.gates.coverage_adequate for run in runs),
        quality_passed=all(run.gates.quality_passed for run in runs),
        fixed_sample_differences_adequate=all(
            run.gates.fixed_sample_differences_adequate for run in runs
        ),
    )


def _common_fingerprint_v3(runs: tuple[VerifiedValidationRunV3, ...]) -> str | None:
    values = {run.envelope.run_fingerprint for run in runs}
    return next(iter(values)) if len(values) == 1 else None


def _assess_v3(
    *,
    runs: tuple[VerifiedValidationRunV3, ...],
    context: V3EvidenceTrustContext,
    evidence_relative_path: str,
    evidence_sha256: str,
    created_at: UtcTimestamp,
) -> SourceCandidateAssessment:
    if not runs:
        raise ValueError("v3 assessment requires runs")
    first = runs[0].envelope
    gates = _aggregate_gates_v3(runs)
    fingerprints = Counter(run.envelope.run_fingerprint for run in runs)
    common = _common_fingerprint_v3(runs)
    independent = min(
        len({run.identity.run_id for run in runs}),
        len({run.identity.invocation_id for run in runs}),
        len({run.identity.session_id for run in runs}),
        len({(run.envelope.started_at.to_iso(), run.envelope.ended_at.to_iso()) for run in runs}),
    )
    all_real = all(run.evidence_kind is EvidenceKind.REAL for run in runs)
    all_successful = all(
        run.outcome is AttemptOutcomeV3.SUCCEEDED and run.failure is None and run.gates.passed
        for run in runs
    )
    required_cases_met = first.selected_case_ids == context.promotion_profile.required_case_ids
    same_selection = all(run.envelope.selected_case_ids == first.selected_case_ids for run in runs)
    ready = (
        all_real
        and all_successful
        and common is not None
        and same_selection
        and required_cases_met
        and independent == len(runs)
        and len(runs) >= context.promotion_profile.minimum_stability_runs
    )
    reasons: list[str] = []
    if not all_real:
        reasons.append("verifier-trusted REAL runner evidence is required")
    for run in runs:
        if run.outcome is AttemptOutcomeV3.FAILED and run.failure is not None:
            reasons.append(f"trusted attempt failed: {run.failure.category}")
    for field, passed in gates.to_dict().items():
        if not passed:
            reasons.append(f"{field} is false")
    if common is None or len(fingerprints) != 1:
        reasons.append("run fingerprint drift")
    if not same_selection:
        reasons.append("selected case set drift")
    if not required_cases_met:
        reasons.append("selected case IDs do not satisfy the promotion profile")
    if independent != len(runs):
        reasons.append("independent invocation/session evidence is required")
    if len(runs) < context.promotion_profile.minimum_stability_runs:
        reasons.append("promotion profile stability requirement is not met")
    return SourceCandidateAssessment(
        assessment_id=first.identity.assessment_id,
        source=first.source,
        dataset=first.dataset,
        evidence_kind=EvidenceKind.REAL if all_real else EvidenceKind.SYNTHETIC,
        semantic_contract_verified=gates.semantic_contract_verified,
        schema_unit_verified=gates.schema_unit_verified,
        coverage_adequate=gates.coverage_adequate,
        quality_passed=gates.quality_passed,
        fixed_sample_differences_adequate=gates.fixed_sample_differences_adequate,
        stability_runs=independent,
        status=(
            AssessmentStatus.PRODUCTION_READY
            if ready
            else AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION
        ),
        blocking_reason=None if ready else "; ".join(dict.fromkeys(reasons)),
        assessment_config_version=first.versions.config_version,
        sample_spec_version=first.sample_spec_version,
        rule_version=first.versions.rule_version,
        evidence_relative_path=evidence_relative_path,
        evidence_sha256=evidence_sha256,
        created_at=created_at,
    )


def _verify_summary_v3(directory: Path) -> tuple[dict[str, object], str]:
    manifest_bytes = _read_file(directory / "manifest.json")
    complete_bytes = _read_file(directory / "COMPLETE")
    try:
        manifest = json.loads(manifest_bytes)
        complete = json.loads(complete_bytes)
    except json.JSONDecodeError as error:
        raise ValidationEvidenceVerificationError("v3 assessment manifest is invalid") from error
    if not isinstance(manifest, dict) or manifest_bytes != canonical_json(manifest):
        raise ValidationEvidenceVerificationError("v3 assessment manifest must be canonical")
    unsigned = dict(manifest)
    embedded = unsigned.pop("manifest_sha256", None)
    required = {
        "assessment_id",
        "dataset",
        "gates",
        "manifest_schema_version",
        "manifest_sha256",
        "promotion_profile_sha256",
        "promotion_profile_version",
        "reference_bundle_id",
        "reference_bundle_manifest_sha256",
        "run_fingerprint",
        "runs",
        "sample_spec_sha256",
        "sample_spec_version",
        "selected_case_ids",
        "source",
        "source_dataset_contract_sha256",
        "source_dataset_contract_version",
    }
    if (
        set(manifest) != required
        or manifest.get("manifest_schema_version") != ASSESSMENT_SCHEMA_V3
        or not isinstance(embedded, str)
        or compute_sha256(canonical_json(unsigned)) != embedded
        or complete != {"manifest_sha256": embedded}
        or {
            item.relative_to(directory).as_posix()
            for item in directory.rglob("*")
            if item.is_file()
        }
        != {"manifest.json", "COMPLETE"}
    ):
        raise ValidationEvidenceVerificationError("v3 assessment manifest verification failed")
    return manifest, embedded


def _read_file(path: Path) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        return path.read_bytes()
    except OSError as error:
        raise ValidationEvidenceVerificationError("required v3 evidence file is missing") from error


__all__ = [
    "V3EvidenceTrustContext",
    "ValidationEvidenceVerifierV3",
    "ValidationEvidenceWriterV3",
    "ValidationRunEnvelopeV3",
    "VerifiedAssessmentEvidenceV3",
    "VerifiedValidationRunV3",
]
