"""Immutable validation evidence writer and strict verifier."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from quantking.common.ids import parse_entity_id
from quantking.common.paths import is_reparse_point, resolve_under
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.warehouse.manifest import (
    WAREHOUSE_COMPLETE_FILENAME,
    WAREHOUSE_MANIFEST_FILENAME,
    compute_sha256,
    path_safe_id,
    validate_managed_relative,
)
from quantking.data.assessment.models import EvidenceKind
from quantking.data.models import DatasetKind
from quantking.data.validation.models import (
    _VERIFICATION_SEAL,
    AssessmentGates,
    RealSourceValidationRun,
    ValidationFailure,
    ValidationRunEvidence,
    ValidationVersions,
    VerifiedAssessmentEvidence,
)
from quantking.data.validation.service import assess_verified_runs

_MANIFEST_SCHEMA = "source-validation-manifest.v1"


class ValidationEvidenceConflictError(Exception):
    """An immutable validation ID was reused with different content."""


class ValidationEvidenceVerificationError(Exception):
    """Validation evidence failed strict structural or cryptographic verification."""


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
        "canonical_content_sha256": run.canonical_content_sha256,
        "dataset": run.dataset.value,
        "difference_report_id": (
            str(run.difference_report_id) if run.difference_report_id is not None else None
        ),
        "difference_report_sha256": run.difference_report_sha256,
        "ended_at": run.ended_at.to_iso(),
        "evidence_kind": run.evidence_kind.value,
        "failure": (
            {"category": run.failure.category, "retryable": run.failure.retryable}
            if run.failure is not None
            else None
        ),
        "gates": run.gates.to_dict(),
        "quality_manifest_sha256": run.quality_manifest_sha256,
        "quality_run_id": str(run.quality_run_id),
        "raw_manifest_relative_path": run.raw_manifest_relative_path,
        "raw_manifest_sha256": run.raw_manifest_sha256,
        "request_sha256": run.request_sha256,
        "run_id": str(run.run_id),
        "sample_spec_sha256": run.sample_spec_sha256,
        "source": run.source,
        "started_at": run.started_at.to_iso(),
        "versions": run.versions.to_dict(),
    }


class ValidationEvidenceWriter:
    """Write one run via sibling ``.incomplete`` and atomic completion."""

    def __init__(self, *, validation_root: Path) -> None:
        self._root = _validate_root(validation_root)

    def write(self, *, run: ValidationRunEvidence, files: Mapping[str, tuple[bytes, int]]) -> Path:
        if run.evidence_kind is EvidenceKind.REAL and not isinstance(run, RealSourceValidationRun):
            raise TypeError("REAL evidence requires RealSourceValidationRun")
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
            "manifest_schema_version": _MANIFEST_SCHEMA,
            **_run_payload(run),
        }
        manifest_sha = compute_sha256(_canonical(unsigned))
        signed = {**unsigned, "manifest_sha256": manifest_sha}
        manifest_bytes = _canonical(signed)
        complete_bytes = _canonical({"manifest_sha256": manifest_sha})
        directory = self._directory(run)
        if directory.exists():
            if (directory / WAREHOUSE_MANIFEST_FILENAME).read_bytes() == manifest_bytes and (
                directory / WAREHOUSE_COMPLETE_FILENAME
            ).read_bytes() == complete_bytes:
                ValidationEvidenceVerifier(validation_root=self._root).verify(directory=directory)
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
            # Failure evidence is retained in the .incomplete directory; callers may
            # inspect a stable failure category without exposing exception text.
            if incomplete.exists():
                failure = _canonical({"category": "EVIDENCE_WRITE_FAILURE", "retryable": False})
                with suppress(OSError):
                    (incomplete / "failure.json").write_bytes(failure)
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
        """Publish an immutable aggregate over independently completed run directories."""
        verifier = ValidationEvidenceVerifier(validation_root=self._root)
        runs = _verified_child_runs(verifier, assessment_directory)
        if not runs:
            raise ValueError("assessment requires at least one completed validation run")
        first = runs[0]
        gates = _aggregate_gates(tuple(item.verified_runs[0].gates for item in runs))
        unsigned = {
            "assessment_id": str(first.assessment.assessment_id),
            "dataset": first.assessment.dataset.value,
            "evidence_kind": first.assessment.evidence_kind.value,
            "gates": gates.to_dict(),
            "manifest_schema_version": "source-assessment-manifest.v1",
            "runs": [
                {
                    "directory": item.evidence_relative_path.rsplit("/", 1)[1],
                    "manifest_sha256": item.evidence_manifest_sha256,
                    "run_id": str(item.verified_runs[0].run_id),
                }
                for item in runs
            ],
            "source": first.assessment.source,
            "versions": {
                "assessment_config_version": first.assessment.assessment_config_version,
                "rule_version": first.assessment.rule_version,
                "sample_spec_version": first.assessment.sample_spec_version,
            },
        }
        manifest_sha = compute_sha256(_canonical(unsigned))
        signed = _canonical({**unsigned, "manifest_sha256": manifest_sha})
        complete = _canonical({"manifest_sha256": manifest_sha})
        directory = assessment_directory / "_assessment"
        if directory.exists():
            if (directory / WAREHOUSE_MANIFEST_FILENAME).read_bytes() == signed and (
                directory / WAREHOUSE_COMPLETE_FILENAME
            ).read_bytes() == complete:
                return verifier.verify_assessment(assessment_directory=assessment_directory)
            raise ValidationEvidenceConflictError(
                "assessment is already finalized with different run evidence"
            )
        incomplete = assessment_directory / "_assessment.incomplete"
        incomplete.mkdir(parents=False, exist_ok=False)
        (incomplete / WAREHOUSE_MANIFEST_FILENAME).write_bytes(signed)
        (incomplete / WAREHOUSE_COMPLETE_FILENAME).write_bytes(complete)
        os.replace(incomplete, directory)
        return verifier.verify_assessment(assessment_directory=assessment_directory)


class ValidationEvidenceVerifier:
    def __init__(self, *, validation_root: Path) -> None:
        self._root = _validate_root(validation_root)

    def verify(self, *, directory: Path) -> VerifiedAssessmentEvidence:
        try:
            relative_directory = directory.relative_to(self._root)
            resolved = resolve_under(self._root, relative_directory)
            if not resolved.is_dir():
                raise OSError
        except (OSError, ValueError) as error:
            raise ValidationEvidenceVerificationError(
                "validation directory is outside the managed root"
            ) from error
        if is_reparse_point(directory):
            raise ValidationEvidenceVerificationError("validation directory is a symlink/junction")
        manifest_bytes = self._read(directory / WAREHOUSE_MANIFEST_FILENAME)
        complete_bytes = self._read(directory / WAREHOUSE_COMPLETE_FILENAME)
        try:
            manifest = json.loads(manifest_bytes)
            complete = json.loads(complete_bytes)
        except json.JSONDecodeError as error:
            raise ValidationEvidenceVerificationError(
                "manifest or COMPLETE is invalid JSON"
            ) from error
        if not isinstance(manifest, dict) or not isinstance(complete, dict):
            raise ValidationEvidenceVerificationError("manifest and COMPLETE must be objects")
        if manifest_bytes != _canonical(manifest):
            raise ValidationEvidenceVerificationError("manifest is not canonical JSON")
        embedded = manifest.get("manifest_sha256")
        unsigned = dict(manifest)
        unsigned.pop("manifest_sha256", None)
        if (
            not isinstance(embedded, str)
            or compute_sha256(_canonical(unsigned)) != embedded
            or complete != {"manifest_sha256": embedded}
        ):
            raise ValidationEvidenceVerificationError(
                "manifest self-signature or COMPLETE mismatch"
            )
        if manifest.get("manifest_schema_version") != _MANIFEST_SCHEMA:
            raise ValidationEvidenceVerificationError("unsupported validation manifest schema")
        required_fields = {
            "assessment_id",
            "canonical_content_sha256",
            "dataset",
            "difference_report_id",
            "difference_report_sha256",
            "ended_at",
            "evidence_kind",
            "failure",
            "files",
            "gates",
            "manifest_schema_version",
            "manifest_sha256",
            "quality_manifest_sha256",
            "quality_run_id",
            "raw_manifest_relative_path",
            "raw_manifest_sha256",
            "request_sha256",
            "run_id",
            "sample_spec_sha256",
            "source",
            "started_at",
            "versions",
        }
        if set(manifest) != required_fields:
            raise ValidationEvidenceVerificationError(
                "validation manifest fields do not match the schema"
            )
        raw_files = manifest.get("files")
        if not isinstance(raw_files, list):
            raise ValidationEvidenceVerificationError("manifest files must be an array")
        expected = {WAREHOUSE_MANIFEST_FILENAME, WAREHOUSE_COMPLETE_FILENAME}
        files_by_path: dict[str, Mapping[str, object]] = {}
        for raw in raw_files:
            if not isinstance(raw, dict) or set(raw) != {
                "relative_path",
                "row_count",
                "sha256",
                "size",
            }:
                raise ValidationEvidenceVerificationError("invalid manifest file entry")
            relative = str(raw["relative_path"])
            try:
                validate_managed_relative(relative, field="validation evidence file")
            except Exception as error:
                raise ValidationEvidenceVerificationError("unsafe manifest file path") from error
            expected.add(relative)
            files_by_path[relative] = raw
            try:
                absolute = resolve_under(directory, relative)
            except Exception as error:
                raise ValidationEvidenceVerificationError(
                    "unsafe validation evidence path"
                ) from error
            payload = self._read(absolute)
            if (
                len(payload) != raw["size"]
                or compute_sha256(payload) != raw["sha256"]
                or not isinstance(raw["row_count"], int)
                or isinstance(raw["row_count"], bool)
                or raw["row_count"] < 0
            ):
                raise ValidationEvidenceVerificationError("validation evidence file mismatch")
        raw_path = str(manifest["raw_manifest_relative_path"])
        if (
            raw_path not in files_by_path
            or files_by_path[raw_path]["sha256"] != manifest["raw_manifest_sha256"]
        ):
            raise ValidationEvidenceVerificationError("raw manifest lineage mismatch")
        if (
            "canonical/content.json" not in files_by_path
            or files_by_path["canonical/content.json"]["sha256"]
            != manifest["canonical_content_sha256"]
        ):
            raise ValidationEvidenceVerificationError("Canonical content lineage mismatch")
        if (
            "quality/result.json" not in files_by_path
            or files_by_path["quality/result.json"]["sha256"] != manifest["quality_manifest_sha256"]
        ):
            raise ValidationEvidenceVerificationError("quality result lineage mismatch")
        if manifest["difference_report_id"] is not None:
            difference = files_by_path.get("difference/manifest.json")
            if difference is None or difference["sha256"] != manifest["difference_report_sha256"]:
                raise ValidationEvidenceVerificationError("DifferenceReport lineage mismatch")
        actual = {
            item.relative_to(directory).as_posix()
            for item in directory.rglob("*")
            if item.is_file()
        }
        if actual != expected:
            raise ValidationEvidenceVerificationError("validation evidence file set mismatch")
        run = self._parse_run(manifest)
        relative = directory.relative_to(self._root).as_posix()
        assessment = assess_verified_runs(
            source=run.source,
            dataset=run.dataset,
            gates=run.gates,
            verified_run_ids=(run.run_id,),
            assessment_config_version=run.versions.config_version,
            sample_spec_version=run.versions.sample_spec_version,
            rule_version=run.versions.rule_version,
            evidence_kind=run.evidence_kind,
            assessment_id=run.assessment_id,
            evidence_relative_path=relative,
            evidence_sha256=embedded,
            created_at=run.ended_at,
        )
        return VerifiedAssessmentEvidence(
            assessment=assessment,
            verified_runs=(run,),
            evidence_manifest_sha256=embedded,
            evidence_relative_path=relative,
            _seal=_VERIFICATION_SEAL,
        )

    def verify_assessment(self, *, assessment_directory: Path) -> VerifiedAssessmentEvidence:
        summary = assessment_directory / "_assessment"
        manifest_bytes = self._read(summary / WAREHOUSE_MANIFEST_FILENAME)
        complete_bytes = self._read(summary / WAREHOUSE_COMPLETE_FILENAME)
        try:
            manifest = json.loads(manifest_bytes)
            complete = json.loads(complete_bytes)
        except json.JSONDecodeError as error:
            raise ValidationEvidenceVerificationError(
                "assessment manifest or COMPLETE is invalid JSON"
            ) from error
        if not isinstance(manifest, dict) or manifest_bytes != _canonical(manifest):
            raise ValidationEvidenceVerificationError(
                "assessment manifest must be a canonical JSON object"
            )
        embedded = manifest.get("manifest_sha256")
        unsigned = dict(manifest)
        unsigned.pop("manifest_sha256", None)
        if (
            manifest.get("manifest_schema_version") != "source-assessment-manifest.v1"
            or not isinstance(embedded, str)
            or compute_sha256(_canonical(unsigned)) != embedded
            or complete != {"manifest_sha256": embedded}
        ):
            raise ValidationEvidenceVerificationError(
                "assessment manifest self-signature or COMPLETE mismatch"
            )
        if set(manifest) != {
            "assessment_id",
            "dataset",
            "evidence_kind",
            "gates",
            "manifest_schema_version",
            "manifest_sha256",
            "runs",
            "source",
            "versions",
        }:
            raise ValidationEvidenceVerificationError(
                "assessment manifest fields do not match the schema"
            )
        if {
            item.relative_to(summary).as_posix() for item in summary.rglob("*") if item.is_file()
        } != {WAREHOUSE_MANIFEST_FILENAME, WAREHOUSE_COMPLETE_FILENAME}:
            raise ValidationEvidenceVerificationError("assessment summary file set mismatch")
        declared = manifest.get("runs")
        if not isinstance(declared, list) or not declared:
            raise ValidationEvidenceVerificationError("assessment runs must be a non-empty array")
        verified: list[VerifiedAssessmentEvidence] = []
        declared_directories = {
            str(item.get("directory")) for item in declared if isinstance(item, dict)
        }
        actual_directories = {
            child.name
            for child in assessment_directory.iterdir()
            if child.is_dir()
            and child.name != "_assessment"
            and not child.name.endswith(".incomplete")
        }
        if declared_directories != actual_directories:
            raise ValidationEvidenceVerificationError(
                "assessment run directory set does not match the manifest"
            )
        for item in declared:
            if not isinstance(item, dict) or set(item) != {
                "directory",
                "manifest_sha256",
                "run_id",
            }:
                raise ValidationEvidenceVerificationError("assessment run entry is invalid")
            directory_name = str(item["directory"])
            validate_managed_relative(directory_name, field="assessment run directory")
            run_evidence = self.verify(directory=assessment_directory / directory_name)
            run = run_evidence.verified_runs[0]
            if (
                str(run.run_id) != item["run_id"]
                or run_evidence.evidence_manifest_sha256 != item["manifest_sha256"]
            ):
                raise ValidationEvidenceVerificationError("assessment run lineage mismatch")
            verified.append(run_evidence)
        first = verified[0]
        runs = tuple(item.verified_runs[0] for item in verified)
        if len({run.run_id for run in runs}) != len(runs):
            raise ValidationEvidenceVerificationError("assessment run IDs must be distinct")
        if any(
            run.assessment_id != first.assessment.assessment_id
            or run.source != first.assessment.source
            or run.dataset != first.assessment.dataset
            or run.evidence_kind is not first.assessment.evidence_kind
            for run in runs
        ):
            raise ValidationEvidenceVerificationError(
                "assessment runs do not bind one source x dataset cell"
            )
        gates = _aggregate_gates(tuple(run.gates for run in runs))
        versions = manifest.get("versions")
        if not isinstance(versions, dict):
            raise ValidationEvidenceVerificationError("assessment versions are invalid")
        first_run = runs[0]
        expected_versions = {
            "assessment_config_version": first_run.versions.config_version,
            "rule_version": first_run.versions.rule_version,
            "sample_spec_version": first_run.versions.sample_spec_version,
        }
        if versions != expected_versions or any(
            run.versions.config_version != first_run.versions.config_version
            or run.versions.rule_version != first_run.versions.rule_version
            or run.versions.sample_spec_version != first_run.versions.sample_spec_version
            for run in runs
        ):
            raise ValidationEvidenceVerificationError(
                "assessment version lineage does not match verified runs"
            )
        assessment = assess_verified_runs(
            source=first.assessment.source,
            dataset=first.assessment.dataset,
            gates=gates,
            verified_run_ids=tuple(run.run_id for run in runs),
            assessment_config_version=str(versions.get("assessment_config_version", "")),
            sample_spec_version=str(versions.get("sample_spec_version", "")),
            rule_version=str(versions.get("rule_version", "")),
            evidence_kind=first.assessment.evidence_kind,
            assessment_id=first.assessment.assessment_id,
            evidence_relative_path=summary.relative_to(self._root).as_posix(),
            evidence_sha256=embedded,
            created_at=max((run.ended_at for run in runs), key=lambda item: item.value),
        )
        if (
            manifest.get("assessment_id") != str(assessment.assessment_id)
            or manifest.get("source") != assessment.source
            or manifest.get("dataset") != assessment.dataset.value
            or manifest.get("evidence_kind") != assessment.evidence_kind.value
            or manifest.get("gates") != gates.to_dict()
        ):
            raise ValidationEvidenceVerificationError(
                "assessment summary does not match verified runs"
            )
        return VerifiedAssessmentEvidence(
            assessment=assessment,
            verified_runs=runs,
            evidence_manifest_sha256=embedded,
            evidence_relative_path=summary.relative_to(self._root).as_posix(),
            _seal=_VERIFICATION_SEAL,
        )

    @staticmethod
    def _read(path: Path) -> bytes:
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError
            return path.read_bytes()
        except OSError as error:
            raise ValidationEvidenceVerificationError(
                "required evidence file is missing"
            ) from error

    @staticmethod
    def _parse_run(manifest: Mapping[str, Any]) -> ValidationRunEvidence:
        try:
            versions = ValidationVersions(**manifest["versions"])
            gates = AssessmentGates(**manifest["gates"])
            raw_failure = manifest["failure"]
            failure = (
                ValidationFailure(
                    category=str(raw_failure["category"]),
                    retryable=raw_failure["retryable"],
                )
                if raw_failure is not None
                else None
            )
            difference_id = manifest["difference_report_id"]
            evidence_kind = EvidenceKind(str(manifest["evidence_kind"]))
            run_type = (
                RealSourceValidationRun
                if evidence_kind is EvidenceKind.REAL
                else ValidationRunEvidence
            )
            return run_type(
                run_id=parse_entity_id(str(manifest["run_id"])),
                assessment_id=parse_entity_id(str(manifest["assessment_id"])),
                source=str(manifest["source"]),
                dataset=DatasetKind(str(manifest["dataset"])),
                evidence_kind=evidence_kind,
                versions=versions,
                request_sha256=str(manifest["request_sha256"]),
                sample_spec_sha256=str(manifest["sample_spec_sha256"]),
                raw_manifest_relative_path=str(manifest["raw_manifest_relative_path"]),
                raw_manifest_sha256=str(manifest["raw_manifest_sha256"]),
                canonical_content_sha256=str(manifest["canonical_content_sha256"]),
                quality_run_id=parse_entity_id(str(manifest["quality_run_id"])),
                quality_manifest_sha256=str(manifest["quality_manifest_sha256"]),
                difference_report_id=(
                    parse_entity_id(str(difference_id)) if difference_id is not None else None
                ),
                difference_report_sha256=manifest["difference_report_sha256"],
                gates=gates,
                started_at=UtcTimestamp.from_iso(str(manifest["started_at"])),
                ended_at=UtcTimestamp.from_iso(str(manifest["ended_at"])),
                failure=failure,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValidationEvidenceVerificationError(
                "validation run contract is invalid"
            ) from error


def _verified_child_runs(
    verifier: ValidationEvidenceVerifier, assessment_directory: Path
) -> list[VerifiedAssessmentEvidence]:
    results: list[VerifiedAssessmentEvidence] = []
    for child in sorted(assessment_directory.iterdir(), key=lambda item: item.name):
        if child.name.startswith("_assessment") or child.name.endswith(".incomplete"):
            continue
        if child.is_dir():
            results.append(verifier.verify(directory=child))
    if results:
        first = results[0].assessment
        if any(
            item.assessment.assessment_id != first.assessment_id
            or item.assessment.source != first.source
            or item.assessment.dataset != first.dataset
            or item.assessment.evidence_kind is not first.evidence_kind
            or item.assessment.assessment_config_version != first.assessment_config_version
            or item.assessment.sample_spec_version != first.sample_spec_version
            or item.assessment.rule_version != first.rule_version
            for item in results
        ):
            raise ValidationEvidenceVerificationError(
                "completed runs cannot be combined into one assessment"
            )
    return results


def _aggregate_gates(gates: tuple[AssessmentGates, ...]) -> AssessmentGates:
    if not gates:
        raise ValueError("at least one gate result is required")
    return AssessmentGates(
        semantic_contract_verified=all(item.semantic_contract_verified for item in gates),
        schema_unit_verified=all(item.schema_unit_verified for item in gates),
        coverage_adequate=all(item.coverage_adequate for item in gates),
        quality_passed=all(item.quality_passed for item in gates),
        fixed_sample_differences_adequate=all(
            item.fixed_sample_differences_adequate for item in gates
        ),
    )


__all__ = [
    "ValidationEvidenceConflictError",
    "ValidationEvidenceVerificationError",
    "ValidationEvidenceVerifier",
    "ValidationEvidenceWriter",
]
