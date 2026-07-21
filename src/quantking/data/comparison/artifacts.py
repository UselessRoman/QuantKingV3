"""Managed, immutable DifferenceReport artifact writer (S1-04B).

The writer produces a managed directory under
``<artifacts_root>/data-diff/<report_id>/`` that mirrors the Raw,
Canonical and Quality artifact conventions:

* ``manifest.json`` — deterministic JSON embedding ``manifest_sha256``.
* ``COMPLETE`` — JSON marker binding the manifest SHA.
* ``row_differences.json`` — the full row-level difference report.

The writer refuses non-absolute roots, symlink/junction traversal,
overwriting an existing report with different content, and arbitrary
absolute paths inside the manifest. Idempotent rewrites of the same
outcome must produce identical file bytes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from quantking.data.adapters.warehouse.manifest import (
    WAREHOUSE_COMPLETE_FILENAME,
    WAREHOUSE_MANIFEST_FILENAME,
    compute_sha256,
    path_safe_id,
)
from quantking.data.comparison.comparator import CrossSourceComparisonOutcome

_DATA_DIFF_SUBDIR = "data-diff"
_ROW_DIFFERENCES_FILENAME = "row_differences.json"


class DifferenceReportConflictError(Exception):
    """Raised when an existing report directory is overwritten with different content."""


def _validate_artifacts_root(artifacts_root: Path) -> Path:
    if not isinstance(artifacts_root, Path):
        raise TypeError("artifacts_root must be a Path")
    if not artifacts_root.is_absolute():
        raise ValueError("artifacts_root must be an absolute path")
    resolved = artifacts_root.resolve(strict=False)
    if resolved != artifacts_root and artifacts_root.exists():
        raise ValueError("artifacts_root must not be a symlink or junction")
    return artifacts_root


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _signed_manifest(payload: dict[str, object]) -> tuple[bytes, str]:
    """Return canonical manifest bytes with ``manifest_sha256`` embedded."""
    without_hash = _canonical_json(payload)
    digest = compute_sha256(without_hash.encode("utf-8"))
    enriched = json.loads(without_hash)
    enriched["manifest_sha256"] = digest
    return _canonical_json(enriched).encode("utf-8"), digest


def _complete_marker_bytes(manifest_sha256: str) -> bytes:
    return _canonical_json({"manifest_sha256": manifest_sha256}).encode("utf-8")


def _manifest_payload(outcome: CrossSourceComparisonOutcome) -> dict[str, object]:
    rows_bytes = _row_differences_bytes(outcome)
    sample_spec_bytes = json.dumps(
        list(outcome.sample_spec), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return {
        "manifest_schema_version": "difference-report-manifest.v1",
        "report_id": str(outcome.report_id),
        "dataset": outcome.dataset.value,
        "schema_version": outcome.schema_version,
        "primary_source": outcome.primary_source.value,
        "fallback_source": outcome.fallback_source.value,
        "primary_version_id": str(outcome.primary_version_id),
        "fallback_version_id": str(outcome.fallback_version_id),
        "primary_batch_id": str(outcome.primary_batch_id),
        "fallback_batch_id": str(outcome.fallback_batch_id),
        "primary_content_sha256": outcome.primary_content_sha256,
        "fallback_content_sha256": outcome.fallback_content_sha256,
        "primary_quality_run_id": str(outcome.primary_quality_run_id),
        "fallback_quality_run_id": str(outcome.fallback_quality_run_id),
        "sample_spec": list(outcome.sample_spec),
        "sample_spec_sha256": compute_sha256(sample_spec_bytes),
        "threshold_rule_version": outcome.threshold_rule_version,
        "created_at": outcome.created_at.to_iso(),
        "total_compared": outcome.total_compared,
        "total_differences": outcome.total_differences,
        "field_differences": dict(outcome.field_differences),
        "report_sha256": outcome.report_sha256,
        "row_differences_file": _ROW_DIFFERENCES_FILENAME,
        "row_differences_file_sha256": compute_sha256(rows_bytes),
        "row_differences_file_size": len(rows_bytes),
    }


def _row_differences_bytes(outcome: CrossSourceComparisonOutcome) -> bytes:
    payload = [diff.to_dict() for diff in outcome.row_differences]
    return _canonical_json({"rows": payload}).encode("utf-8")


class DifferenceReportArtifactWriter:
    """Write a :class:`CrossSourceComparisonOutcome` to a managed directory."""

    def __init__(self, *, artifacts_root: Path) -> None:
        self._root = _validate_artifacts_root(artifacts_root)

    @property
    def artifacts_root(self) -> Path:
        return self._root

    def write(
        self,
        *,
        outcome: CrossSourceComparisonOutcome,
        row_evidence_bytes: bytes | None = None,
    ) -> Path:
        """Persist ``outcome`` and return the managed directory.

        ``row_evidence_bytes`` is an optional opaque blob supplied by the
        caller; when ``None`` the writer serializes the outcome's
        ``row_differences`` to JSON. Idempotent rewrites of the same
        outcome succeed without modifying any file bytes.
        """
        if not isinstance(outcome, CrossSourceComparisonOutcome):
            raise TypeError("outcome must be a CrossSourceComparisonOutcome")
        directory = self._directory_for(outcome.report_id)
        manifest_payload = _manifest_payload(outcome)
        manifest_bytes, manifest_sha = _signed_manifest(manifest_payload)
        complete_bytes = _complete_marker_bytes(manifest_sha)
        if row_evidence_bytes is not None:
            raise ValueError("row_evidence_bytes is not accepted; evidence is derived from outcome")
        rows_bytes = _row_differences_bytes(outcome)
        if directory.exists():
            self._verify_idempotent(directory, manifest_bytes, rows_bytes, outcome)
            return directory
        incomplete = directory.with_name(directory.name + ".incomplete")
        incomplete.mkdir(parents=True, exist_ok=False)
        try:
            (incomplete / _ROW_DIFFERENCES_FILENAME).write_bytes(rows_bytes)
            (incomplete / WAREHOUSE_MANIFEST_FILENAME).write_bytes(manifest_bytes)
            (incomplete / WAREHOUSE_COMPLETE_FILENAME).write_bytes(complete_bytes)
            os.replace(incomplete, directory)
        except Exception:
            if incomplete.exists():
                _remove_tree(incomplete)
            raise
        return directory

    def _directory_for(self, report_id: object) -> Path:
        directory_name = path_safe_id(str(report_id))
        candidate = self._root / _DATA_DIFF_SUBDIR / directory_name
        resolved = candidate.resolve(strict=False)
        root_resolved = self._root.resolve(strict=False)
        try:
            resolved.relative_to(root_resolved)
        except ValueError as error:
            raise ValueError("artifact directory escapes the managed artifacts_root") from error
        return candidate

    def _verify_idempotent(
        self,
        directory: Path,
        manifest_bytes: bytes,
        rows_bytes: bytes,
        outcome: CrossSourceComparisonOutcome,
    ) -> None:
        manifest_path = directory / WAREHOUSE_MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise DifferenceReportConflictError(
                "existing artifact directory is missing manifest.json",
            )
        existing_manifest = manifest_path.read_bytes()
        if existing_manifest != manifest_bytes:
            raise DifferenceReportConflictError(
                "existing artifact directory has a different manifest",
            )
        rows_path = directory / _ROW_DIFFERENCES_FILENAME
        if not rows_path.is_file():
            raise DifferenceReportConflictError(
                "existing artifact directory is missing row_differences.json",
            )
        if rows_path.read_bytes() != rows_bytes:
            raise DifferenceReportConflictError(
                "existing artifact directory has different row evidence",
            )
        complete_path = directory / WAREHOUSE_COMPLETE_FILENAME
        if not complete_path.is_file():
            raise DifferenceReportConflictError(
                "existing artifact directory is missing COMPLETE marker",
            )
        try:
            marker = json.loads(complete_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DifferenceReportConflictError("existing COMPLETE marker is invalid") from error
        if marker != {"manifest_sha256": _manifest_sha_from_signed(existing_manifest)}:
            raise DifferenceReportConflictError("existing COMPLETE marker is not bound to manifest")


def _manifest_sha_from_signed(manifest_bytes: bytes) -> str:
    payload = json.loads(manifest_bytes.decode("utf-8"))
    embedded = payload.pop("manifest_sha256", None)
    if not isinstance(embedded, str):
        raise DifferenceReportConflictError("manifest_sha256 is missing")
    return compute_sha256(_canonical_json(payload).encode("utf-8"))


def _remove_tree(path: Path) -> None:
    if path.is_dir():
        for child in path.iterdir():
            _remove_tree(child)
        path.rmdir()
    else:
        path.unlink(missing_ok=True)


__all__ = [
    "DifferenceReportArtifactWriter",
    "DifferenceReportConflictError",
]
