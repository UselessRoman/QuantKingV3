"""Managed, immutable quality-run artifact writer (S1-04A).

The writer produces a managed directory under
``<artifacts_root>/data-quality/<run_id>/`` that mirrors the Raw and
Canonical warehouse conventions:

* ``manifest.json`` — deterministic JSON embedding ``manifest_sha256``.
* ``COMPLETE`` — JSON marker binding the manifest SHA.
* ``failure.json`` — sanitized failure summary on FAILED runs.
* per-rule evidence files supplied by the caller.

The writer refuses non-absolute roots, symlink/junction traversal,
overwriting an existing run with different content, and arbitrary
absolute paths inside the manifest. Idempotent rewrites of the same
outcome must produce identical file bytes.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from quantking.data.adapters.warehouse.manifest import (
    WAREHOUSE_COMPLETE_FILENAME,
    WAREHOUSE_FAILURE_FILENAME,
    WAREHOUSE_MANIFEST_FILENAME,
    compute_sha256,
    path_safe_id,
    validate_managed_relative,
)
from quantking.data.quality.models import QualityRunArtifact

_DATA_QUALITY_SUBDIR = "data-quality"
_MANAGED_RELATIVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./=-]*\Z")
_ABSOLUTE_PATH_RE = re.compile(r"[A-Za-z]:[\\/]|^/")


def _validate_artifacts_root(artifacts_root: Path) -> Path:
    if not isinstance(artifacts_root, Path):
        raise TypeError("artifacts_root must be a Path")
    if not artifacts_root.is_absolute():
        raise ValueError("artifacts_root must be an absolute path")
    resolved = artifacts_root.resolve(strict=False)
    # We allow the path to not yet exist, but it must not be a
    # symlink or junction that resolves elsewhere.
    if resolved != artifacts_root and artifacts_root.exists():
        raise ValueError("artifacts_root must not be a symlink or junction")
    return artifacts_root


def _sanitize_text(value: str) -> str:
    """Strip absolute paths, drive letters and control characters."""
    if not isinstance(value, str):
        return ""
    cleaned = value
    # Replace Windows and POSIX absolute paths with a placeholder.
    cleaned = _ABSOLUTE_PATH_RE.sub("<redacted>", cleaned)
    # Strip control characters.
    cleaned = "".join(ch for ch in cleaned if ord(ch) >= 0x20 or ch == "\t")
    return cleaned


def _manifest_payload(outcome: QualityRunArtifact) -> dict[str, object]:
    return {
        "run_id": str(outcome.run_id),
        "version_id": str(outcome.version_id),
        "rule_version": outcome.rule_version,
        "schema_version": outcome.schema_version,
        "content_sha256": outcome.content_sha256,
        "started_at": outcome.started_at.to_iso(),
        "ended_at": outcome.ended_at.to_iso(),
        "status": outcome.status.value,
        "results": [
            {
                "rule_code": result.rule_code,
                "status": result.status.value,
                "affected_count": result.affected_count,
                "summary": result.summary,
                "row_evidence_relative_path": result.row_evidence_relative_path,
            }
            for result in outcome.results
        ],
    }


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _signed_manifest(payload: Mapping[str, object]) -> tuple[bytes, str]:
    """Return canonical manifest bytes with ``manifest_sha256`` embedded.

    The SHA-256 is computed from the canonical JSON *without* the
    ``manifest_sha256`` field so the value can be embedded without
    creating a circular dependency. The COMPLETE marker must bind to
    the same embedded digest, not to a re-hash of the signed bytes.
    """
    without_hash = _canonical_json(payload)
    digest = compute_sha256(without_hash.encode("utf-8"))
    enriched = json.loads(without_hash)
    enriched["manifest_sha256"] = digest
    return _canonical_json(enriched).encode("utf-8"), digest


def _complete_marker_bytes(manifest_sha256: str) -> bytes:
    return _canonical_json({"manifest_sha256": manifest_sha256}).encode("utf-8")


def _failure_payload(outcome: QualityRunArtifact) -> dict[str, str]:
    if outcome.failure is None:
        return {}
    return {
        "error_type": outcome.failure.error_type.value,
        "message": _sanitize_text(outcome.failure.message),
        "retryable": str(outcome.failure.retryable).lower(),
    }


def _row_evidence_relative_path(rule_code: str) -> str:
    validate_managed_relative(f"{rule_code}.parquet", field="row_evidence_relative_path")
    return f"{rule_code}.parquet"


class QualityArtifactWriter:
    """Write a :class:`QualityRunArtifact` to a managed directory."""

    def __init__(self, *, artifacts_root: Path) -> None:
        self._root = _validate_artifacts_root(artifacts_root)

    @property
    def artifacts_root(self) -> Path:
        return self._root

    def write(
        self,
        *,
        outcome: QualityRunArtifact,
        row_evidence_tables: Mapping[str, pa.Table],
    ) -> Path:
        """Persist ``outcome`` and return the managed directory.

        Idempotent rewrites of the *same* outcome (same ``run_id`` and
        same content) succeed without modifying any file bytes. A
        different outcome reusing an existing ``run_id`` raises
        :class:`QualityArtifactConflictError`.
        """
        if not isinstance(outcome, QualityRunArtifact):
            raise TypeError("outcome must be a QualityRunArtifact")
        if not isinstance(row_evidence_tables, Mapping):
            raise TypeError("row_evidence_tables must be a mapping")
        for key, table in row_evidence_tables.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("row_evidence_tables key must be non-empty text")
            if not isinstance(table, pa.Table):
                raise TypeError("row_evidence_tables value must be a pa.Table")
            validate_managed_relative(
                _row_evidence_relative_path(key), field="row_evidence_relative_path"
            )
        directory = self._directory_for(outcome.run_id)
        manifest_payload = _manifest_payload(outcome)
        manifest_bytes, manifest_sha = _signed_manifest(manifest_payload)
        complete_bytes = _complete_marker_bytes(manifest_sha)
        if directory.exists():
            self._verify_idempotent(directory, manifest_bytes, outcome)
            return directory
        # Build inside a sibling .incomplete directory then atomically rename.
        incomplete = directory.with_name(directory.name + ".incomplete")
        incomplete.mkdir(parents=True, exist_ok=False)
        try:
            for key, table in row_evidence_tables.items():
                relative = _row_evidence_relative_path(key)
                (incomplete / relative).parent.mkdir(parents=True, exist_ok=True)
                pq.write_table(table, incomplete / relative)  # type: ignore[no-untyped-call]
            (incomplete / WAREHOUSE_MANIFEST_FILENAME).write_bytes(manifest_bytes)
            (incomplete / WAREHOUSE_COMPLETE_FILENAME).write_bytes(complete_bytes)
            if outcome.status.value == "FAILED":
                failure_payload = _failure_payload(outcome)
                failure_bytes = _canonical_json(failure_payload).encode("utf-8")
                (incomplete / WAREHOUSE_FAILURE_FILENAME).write_bytes(failure_bytes)
            os.replace(incomplete, directory)
        except Exception:
            # Best-effort cleanup of the .incomplete directory on failure.
            if incomplete.exists():
                _remove_tree(incomplete)
            raise
        return directory

    def _directory_for(self, run_id: object) -> Path:
        directory_name = path_safe_id(str(run_id))
        candidate = self._root / _DATA_QUALITY_SUBDIR / directory_name
        resolved = candidate.resolve(strict=False)
        # The resolved path must remain under the managed root.
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
        outcome: QualityRunArtifact,
    ) -> None:
        manifest_path = directory / WAREHOUSE_MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise QualityArtifactConflictError(
                "existing artifact directory is missing manifest.json",
                context={"directory": str(directory)},
            )
        existing = manifest_path.read_bytes()
        if existing != manifest_bytes:
            raise QualityArtifactConflictError(
                "an artifact with the same run_id but different content already exists",
                context={"directory": str(directory)},
            )
        complete_path = directory / WAREHOUSE_COMPLETE_FILENAME
        if not complete_path.is_file():
            raise QualityArtifactConflictError(
                "existing artifact directory is missing COMPLETE",
                context={"directory": str(directory)},
            )
        complete = json.loads(complete_path.read_text(encoding="utf-8"))
        expected_sha = json.loads(manifest_bytes.decode("utf-8"))["manifest_sha256"]
        if complete.get("manifest_sha256") != expected_sha:
            raise QualityArtifactConflictError(
                "existing COMPLETE marker does not bind the manifest SHA",
                context={"directory": str(directory)},
            )


def _remove_tree(path: Path) -> None:
    if path.is_dir():
        for entry in path.iterdir():
            _remove_tree(entry)
        path.rmdir()
    elif path.exists():
        path.unlink()


class QualityArtifactConflictError(Exception):
    """Raised when an artifact directory cannot be written or verified.

    Accepts an optional ``context`` mapping for diagnostic key/value pairs
    (e.g. the offending directory path), mirroring the
    :class:`quantking.common.errors.QuantKingError` convention without
    forcing a dependency on the common error base class.
    """

    def __init__(
        self,
        message: str,
        *,
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.context: Mapping[str, str] = context or {}


__all__: tuple[str, ...] = (
    "QualityArtifactConflictError",
    "QualityArtifactWriter",
)
