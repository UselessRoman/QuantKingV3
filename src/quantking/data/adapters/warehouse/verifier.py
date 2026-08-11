"""Unified strict published-directory verifier for Raw and Canonical warehouses.

This module is the single source of truth for verifying that a published
directory on disk is internally consistent and has not been tampered with.
Raw publish reconciliation, Canonical publish reconciliation, and the
DuckDB query adapter all route through the same verification path so that
a directory declared queryable by one component is guaranteed to pass the
same checks in every other component.

Each verification call re-validates, in order:

1. The directory is contained inside the resolved ``data_root`` and no
   intermediate path component is a symlink or Windows junction/reparse
   point.
2. The manifest file exists and its raw bytes are canonical deterministic
   JSON (sorted keys, fixed separators, no whitespace).
3. The manifest schema version is the expected one.
4. The manifest's embedded ``manifest_sha256`` field exists, is a
   64-character lowercase hex string, and equals the SHA-256 of the
   canonical JSON with the field itself removed.
5. The ``COMPLETE`` marker exists, is a JSON object, and its
   ``manifest_sha256`` (and ``version_id``/``batch_id`` where applicable)
   exactly match the manifest.
6. The manifest ``files`` list has no duplicate ``relative_path`` entries
   and every entry has the required typed fields.
7. The on-disk file set exactly matches the manifest file set: no missing
   files, no extra files, no managed-marker drift.
8. For every managed file the on-disk size, SHA-256 and (for canonical)
   row count match the manifest, and the Parquet schema is consistent
   with the dataset contract.

Any inconsistency raises :class:`ManifestIntegrityError`. The verifier
never deletes, overwrites or auto-repairs the directory; recovery is the
caller's responsibility and must use a new task.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from quantking.common.errors import InvalidPathError
from quantking.common.paths import is_reparse_point
from quantking.data.adapters.warehouse.errors import ManifestIntegrityError
from quantking.data.adapters.warehouse.manifest import (
    RAW_INPUT_LISTING_FILENAME,
    RAW_MANIFEST_SCHEMA_VERSION,
    WAREHOUSE_COMPLETE_FILENAME,
    WAREHOUSE_INPUT_LISTING_FILENAME,
    WAREHOUSE_MANIFEST_FILENAME,
    WAREHOUSE_MANIFEST_SCHEMA_VERSION,
    compute_sha256,
    validate_managed_relative,
)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class VerifiedFile:
    """One verified file entry from a published directory."""

    relative_path: str
    absolute_path: Path
    size: int
    sha256: str
    row_count: int
    partition: dict[str, object]


@dataclass(frozen=True, slots=True)
class VerifiedDirectory:
    """The fully verified state of one published directory."""

    manifest: dict[str, object]
    manifest_bytes: bytes
    manifest_sha: str
    files: tuple[VerifiedFile, ...]


# ---------------------------------------------------------------------------
# Data-root and intermediate-path safety
# ---------------------------------------------------------------------------


def verify_data_root(root: Path) -> Path:
    """Return the resolved data root, rejecting symlinks and junctions.

    The data root is the single anchor for every managed path. If it is a
    symlink or Windows reparse point, an attacker could redirect every
    subsequent write outside the managed tree. We therefore refuse to
    construct any warehouse component on top of a linked root.
    """
    if not isinstance(root, Path):
        raise InvalidPathError("data_root must be a Path")
    if not root.is_absolute():
        raise InvalidPathError("data_root must be an absolute path")
    current = Path(root.anchor)
    try:
        for part in root.parts[1:]:
            current /= part
            if current.exists() and is_reparse_point(current):
                raise InvalidPathError(
                    "data_root must not contain a symlink or junction",
                    context={"path": str(root)},
                )
        return root.resolve(strict=False)
    except InvalidPathError:
        raise
    except (OSError, RuntimeError) as error:
        raise InvalidPathError(
            "data_root could not be safely resolved",
            context={"path": str(root)},
        ) from error


def assert_path_under_root(root_resolved: Path, target: Path) -> Path:
    """Assert that ``target`` resolves under ``root_resolved`` with no symlinks.

    Walks every existing intermediate component from the root to the
    target and rejects any symlink or Windows junction/reparse point.
    The final resolved path must remain inside the root.
    """
    if not isinstance(target, Path):
        raise InvalidPathError("target must be a Path")
    if not target.is_absolute():
        raise InvalidPathError("target must be an absolute path")
    try:
        lexical_relative = target.relative_to(root_resolved)
    except ValueError as error:
        raise InvalidPathError(
            "target is not lexically contained by the managed data root",
            context={"target": str(target), "root": str(root_resolved)},
        ) from error
    if ".." in lexical_relative.parts:
        raise InvalidPathError(
            "target must not contain parent traversal",
            context={"target": str(target)},
        )
    current = root_resolved
    try:
        for part in lexical_relative.parts:
            current /= part
            if current.exists() and is_reparse_point(current):
                raise InvalidPathError(
                    "managed path contains a symlink or junction",
                    context={"target": str(target), "intermediate": str(current)},
                )
        resolved = target.resolve(strict=False)
    except InvalidPathError:
        raise
    except (OSError, RuntimeError) as error:
        raise InvalidPathError(
            "target path is not resolvable",
            context={"target": str(target), "detail": str(error)},
        ) from error
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise InvalidPathError(
            "target escapes the managed data root",
            context={"target": str(target), "root": str(root_resolved)},
        )
    return resolved


# ---------------------------------------------------------------------------
# Shared low-level checks
# ---------------------------------------------------------------------------


def _read_canonical_manifest(
    directory: Path, *, expected_schema: str
) -> tuple[bytes, dict[str, object]]:
    """Read manifest bytes and parsed payload, enforcing canonical form."""
    manifest_path = directory / WAREHOUSE_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ManifestIntegrityError(
            "manifest is missing",
            context={"directory": str(directory)},
        )
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ManifestIntegrityError(
            "manifest is not valid JSON",
            context={"detail": str(error)},
        ) from error
    if not isinstance(manifest, dict):
        raise ManifestIntegrityError("manifest must be a JSON object")
    canonical_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if raw != canonical_bytes:
        raise ManifestIntegrityError(
            "manifest bytes are not in canonical deterministic form",
        )
    schema_version = manifest.get("manifest_schema_version")
    if schema_version != expected_schema:
        raise ManifestIntegrityError(
            "manifest schema version is unsupported",
            context={"expected": expected_schema, "actual": str(schema_version)},
        )
    _verify_manifest_shape(manifest, expected_schema=expected_schema)
    return raw, manifest


def _verify_manifest_shape(manifest: dict[str, object], *, expected_schema: str) -> None:
    """Validate all required v1 manifest fields and their stable JSON types."""
    text_fields: tuple[str, ...]
    if expected_schema == RAW_MANIFEST_SCHEMA_VERSION:
        required = {
            "manifest_schema_version",
            "manifest_sha256",
            "code_version",
            "source",
            "dataset",
            "batch_id",
            "adapter_version",
            "created_at",
            "request_params",
            "files",
        }
        text_fields = (
            "manifest_schema_version",
            "manifest_sha256",
            "code_version",
            "source",
            "dataset",
            "batch_id",
            "adapter_version",
            "created_at",
        )
    else:
        required = {
            "manifest_schema_version",
            "manifest_sha256",
            "content_sha256",
            "coverage_end",
            "coverage_start",
            "coverage_summary",
            "created_at",
            "dataset",
            "files",
            "input_batch_ids",
            "parent_version_id",
            "partition_scheme",
            "rule_version",
            "schema_version",
            "code_version",
            "version_id",
        }
        if "resolution" in manifest:
            required.add("resolution")
        text_fields = (
            "manifest_schema_version",
            "manifest_sha256",
            "content_sha256",
            "created_at",
            "dataset",
            "rule_version",
            "schema_version",
            "code_version",
            "version_id",
        )
    actual = set(manifest)
    if actual != required:
        raise ManifestIntegrityError(
            "manifest fields do not match the declared schema",
            context={
                "missing": ",".join(sorted(required - actual)),
                "extra": ",".join(sorted(actual - required)),
            },
        )
    for field in text_fields:
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ManifestIntegrityError(
                "manifest text field is missing or invalid",
                context={"field": field},
            )
    if not isinstance(manifest.get("files"), list):
        raise ManifestIntegrityError("manifest files entry is not a list")
    if expected_schema == RAW_MANIFEST_SCHEMA_VERSION:
        params = manifest.get("request_params")
        if not isinstance(params, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in params.items()
        ):
            raise ManifestIntegrityError("raw request_params must map strings to strings")
        return

    content_sha = manifest.get("content_sha256")
    if not isinstance(content_sha, str) or not _HEX64_RE.match(content_sha):
        raise ManifestIntegrityError("manifest content_sha256 is invalid")
    parent = manifest.get("parent_version_id")
    if parent is not None and (not isinstance(parent, str) or not parent.strip()):
        raise ManifestIntegrityError("manifest parent_version_id is invalid")
    batch_ids = manifest.get("input_batch_ids")
    if (
        not isinstance(batch_ids, list)
        or any(not isinstance(value, str) or not value.strip() for value in batch_ids)
        or len(set(batch_ids)) != len(batch_ids)
    ):
        raise ManifestIntegrityError("manifest input_batch_ids are invalid")
    start = manifest.get("coverage_start")
    end = manifest.get("coverage_end")
    if (start is None) != (end is None):
        raise ManifestIntegrityError("coverage_start and coverage_end must both be set or null")
    for field, value in (("coverage_start", start), ("coverage_end", end)):
        if value is None:
            continue
        if not isinstance(value, str):
            raise ManifestIntegrityError(
                "manifest coverage date must be ISO text",
                context={"field": field},
            )
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ManifestIntegrityError(
                "manifest coverage date is invalid",
                context={"field": field},
            ) from error
    summary = manifest.get("coverage_summary")
    if not isinstance(summary, dict) or any(
        not isinstance(key, str) or not key or not isinstance(value, str) or not value
        for key, value in summary.items()
    ):
        raise ManifestIntegrityError("manifest coverage_summary must map text to text")
    for field in ("row_count", "security_count", "date_count"):
        value = summary.get(field)
        if not isinstance(value, str) or not value.isdecimal():
            raise ManifestIntegrityError(
                "manifest coverage count is invalid",
                context={"field": field},
            )
    partition_scheme = manifest.get("partition_scheme")
    if not isinstance(partition_scheme, dict) or set(partition_scheme) != {
        "scheme_id",
        "scheme_version",
        "partition_keys",
        "date_field",
    }:
        raise ManifestIntegrityError("manifest partition_scheme is invalid")
    if (
        not isinstance(partition_scheme.get("scheme_id"), str)
        or not isinstance(partition_scheme.get("scheme_version"), str)
        or not isinstance(partition_scheme.get("date_field"), str)
    ):
        raise ManifestIntegrityError("manifest partition_scheme text fields are invalid")
    partition_keys = partition_scheme.get("partition_keys")
    if not isinstance(partition_keys, list) or any(
        not isinstance(value, str) or not value for value in partition_keys
    ):
        raise ManifestIntegrityError("manifest partition_keys are invalid")
    if "resolution" in manifest:
        _verify_resolution_manifest(manifest["resolution"], version_id=manifest["version_id"])


def _verify_resolution_manifest(value: object, *, version_id: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "artifact_relative_path",
        "candidate_version_id",
        "created_job_id",
        "difference_reports",
        "input_fingerprint",
        "manifest_sha256",
        "rule_version",
        "run_id",
        "threshold_rule_version",
    }:
        raise ManifestIntegrityError("resolution manifest binding is invalid")
    for field in (
        "artifact_relative_path",
        "candidate_version_id",
        "created_job_id",
        "rule_version",
        "run_id",
        "threshold_rule_version",
    ):
        if not isinstance(value[field], str) or not value[field]:
            raise ManifestIntegrityError("resolution manifest text binding is invalid")
    if value["candidate_version_id"] != version_id:
        raise ManifestIntegrityError("resolution candidate does not match version")
    for field in ("input_fingerprint", "manifest_sha256"):
        if not isinstance(value[field], str) or not _HEX64_RE.match(value[field]):
            raise ManifestIntegrityError("resolution manifest SHA binding is invalid")
    reports = value["difference_reports"]
    if (
        not isinstance(reports, list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"report_id", "artifact_relative_path", "manifest_sha256"}
            or not isinstance(item["report_id"], str)
            or not item["report_id"]
            or not isinstance(item["artifact_relative_path"], str)
            or not item["artifact_relative_path"]
            or not isinstance(item["manifest_sha256"], str)
            or not _HEX64_RE.match(item["manifest_sha256"])
            for item in reports
        )
        or reports
        != sorted(
            reports,
            key=lambda item: item["report_id"],
        )
        or len({item["report_id"] for item in reports}) != len(reports)
    ):
        raise ManifestIntegrityError("resolution DifferenceReport bindings are invalid")


def _verify_manifest_self_sha(manifest: dict[str, object]) -> str:
    """Verify the manifest's embedded ``manifest_sha256`` field."""
    declared = manifest.get("manifest_sha256")
    if not isinstance(declared, str) or not _HEX64_RE.match(declared):
        raise ManifestIntegrityError(
            "manifest is missing a valid manifest_sha256 field",
        )
    without_self_hash = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    expected = compute_sha256(
        json.dumps(
            without_self_hash,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    if expected != declared:
        raise ManifestIntegrityError(
            "manifest SHA-256 does not match the on-disk file",
        )
    return declared


def _verify_complete_marker(
    directory: Path,
    *,
    manifest: dict[str, object],
    manifest_sha: str,
    version_key: str,
    expected_version: str,
) -> dict[str, object]:
    """Verify the COMPLETE marker binds to the manifest SHA and version."""
    complete_path = directory / WAREHOUSE_COMPLETE_FILENAME
    if not complete_path.is_file():
        raise ManifestIntegrityError(
            "COMPLETE marker is missing",
            context={"directory": str(directory)},
        )
    try:
        payload = json.loads(complete_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ManifestIntegrityError(
            "COMPLETE marker is not valid JSON",
            context={"detail": str(error)},
        ) from error
    if not isinstance(payload, dict):
        raise ManifestIntegrityError("COMPLETE marker must be a JSON object")
    complete_version = payload.get(version_key)
    if not isinstance(complete_version, str) or complete_version != expected_version:
        raise ManifestIntegrityError(
            f"COMPLETE marker {version_key} does not match the manifest",
            context={"expected": expected_version},
        )
    complete_sha = payload.get("manifest_sha256")
    if not isinstance(complete_sha, str) or not _HEX64_RE.match(complete_sha):
        raise ManifestIntegrityError(
            "COMPLETE marker is missing a valid manifest_sha256",
        )
    if complete_sha != manifest_sha:
        raise ManifestIntegrityError(
            "COMPLETE manifest_sha256 does not match the manifest",
        )
    return payload


def _verify_file_entries(
    manifest: dict[str, object],
    directory: Path,
    *,
    root_resolved: Path,
    extra_managed: frozenset[str],
    require_parquet_checks: bool,
    expected_schema: dict[str, object] | None = None,
) -> tuple[VerifiedFile, ...]:
    """Verify every manifest file entry and the on-disk file set."""
    listed = manifest.get("files")
    if not isinstance(listed, list):
        raise ManifestIntegrityError("manifest files entry is not a list")
    seen_paths: set[str] = set()
    seen_partitions: set[str] = set()
    declared: list[dict[str, object]] = []
    for entry in listed:
        if not isinstance(entry, dict):
            raise ManifestIntegrityError("manifest file entry is not a dict")
        relative = entry.get("relative_path")
        if not isinstance(relative, str) or not relative.strip():
            raise ManifestIntegrityError("manifest file relative_path is not a non-empty string")
        if relative in seen_paths:
            raise ManifestIntegrityError(
                "manifest contains a duplicate relative_path",
                context={"relative_path": relative},
            )
        seen_paths.add(relative)
        try:
            validate_managed_relative(relative, field="relative_path")
        except InvalidPathError as error:
            raise ManifestIntegrityError(
                "manifest contains an unsafe relative path",
                context={"relative_path": relative, "detail": str(error)},
            ) from error
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ManifestIntegrityError(
                "manifest file size is not a non-negative integer",
                context={"relative_path": relative},
            )
        sha = entry.get("sha256")
        if not isinstance(sha, str) or not _HEX64_RE.match(sha):
            raise ManifestIntegrityError(
                "manifest file sha256 is not a 64-character lowercase hex string",
                context={"relative_path": relative},
            )
        row_count = entry.get("row_count")
        if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
            raise ManifestIntegrityError(
                "manifest file row_count is not a non-negative integer",
                context={"relative_path": relative},
            )
        partition = entry.get("partition", {})
        if not isinstance(partition, dict):
            raise ManifestIntegrityError(
                "manifest file partition is not a JSON object",
                context={"relative_path": relative},
            )
        if require_parquet_checks:
            partition_fingerprint = json.dumps(
                partition,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if partition_fingerprint in seen_partitions:
                raise ManifestIntegrityError(
                    "manifest contains a duplicate partition file entry",
                    context={"relative_path": relative},
                )
            seen_partitions.add(partition_fingerprint)
        declared.append(
            {
                "relative_path": relative,
                "size": size,
                "sha256": sha,
                "row_count": row_count,
                "partition": dict(partition),
            }
        )
    # Resolve each declared file under the root and verify size/SHA/rows.
    verified: list[VerifiedFile] = []
    for entry in declared:
        relative = str(entry["relative_path"])
        absolute = directory / relative
        resolved = assert_path_under_root(root_resolved, absolute)
        if not resolved.is_file():
            raise ManifestIntegrityError(
                "manifest file is missing on disk",
                context={"relative_path": relative},
            )
        payload = resolved.read_bytes()
        size = int(entry["size"])  # type: ignore[call-overload]
        if len(payload) != size:
            raise ManifestIntegrityError(
                "manifest file size does not match on-disk size",
                context={"relative_path": relative},
            )
        digest = compute_sha256(payload)
        if digest != str(entry["sha256"]):
            raise ManifestIntegrityError(
                "manifest file SHA-256 does not match on-disk digest",
                context={"relative_path": relative},
            )
        row_count = int(entry["row_count"])  # type: ignore[call-overload]
        if require_parquet_checks:
            actual_rows = _parquet_row_count(resolved)
            if actual_rows != row_count:
                raise ManifestIntegrityError(
                    "manifest file row_count does not match the Parquet file",
                    context={
                        "relative_path": relative,
                        "expected": str(row_count),
                        "actual": str(actual_rows),
                    },
                )
            if expected_schema is not None:
                _verify_parquet_schema(resolved, expected_schema, relative_path=relative)
        partition = dict(entry["partition"])  # type: ignore[call-overload]
        verified.append(
            VerifiedFile(
                relative_path=relative,
                absolute_path=resolved,
                size=size,
                sha256=str(entry["sha256"]),
                row_count=row_count,
                partition=partition,
            )
        )
    # Walk the directory and reject any unexpected file.
    managed_set = frozenset(
        {
            WAREHOUSE_MANIFEST_FILENAME,
            WAREHOUSE_COMPLETE_FILENAME,
        }
    ).union(extra_managed)
    for absolute in directory.rglob("*"):
        if is_reparse_point(absolute):
            raise ManifestIntegrityError(
                "published directory contains a symlink or junction",
                context={"path": str(absolute)},
            )
        if absolute.is_dir():
            continue
        try:
            managed = absolute.relative_to(directory).as_posix()
        except ValueError as error:
            raise ManifestIntegrityError(
                "file escapes its managed directory",
                context={"path": str(absolute)},
            ) from error
        if managed in managed_set:
            continue
        if not any(item.relative_path == managed for item in verified):
            raise ManifestIntegrityError(
                "directory has an unexpected file",
                context={"relative_path": managed},
            )
    return tuple(verified)


def _parquet_row_count(path: Path) -> int:
    """Return the row count of a Parquet file without reading all rows."""
    from pyarrow import parquet as pa_parquet

    try:
        metadata = pa_parquet.read_metadata(str(path))  # type: ignore[no-untyped-call]
    except Exception as error:
        raise ManifestIntegrityError(
            "Parquet metadata could not be read",
            context={"path": str(path), "detail": str(error)},
        ) from error
    return int(metadata.num_rows)


def _verify_parquet_schema(path: Path, expected: dict[str, object], *, relative_path: str) -> None:
    """Verify a Parquet file's schema matches the expected canonical schema."""
    from pyarrow import parquet as pa_parquet

    try:
        actual = pa_parquet.read_schema(str(path))  # type: ignore[no-untyped-call]
    except Exception as error:
        raise ManifestIntegrityError(
            "Parquet schema could not be read",
            context={"relative_path": relative_path, "detail": str(error)},
        ) from error
    expected_schema = expected.get("schema")
    if expected_schema is None:
        return
    if not actual.equals(expected_schema, check_metadata=False):
        raise ManifestIntegrityError(
            "Parquet schema does not match the dataset contract",
            context={
                "relative_path": relative_path,
                "expected": str(expected_schema),
                "actual": str(actual),
            },
        )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def verify_raw_directory(directory: Path, *, data_root: Path) -> VerifiedDirectory:
    """Strictly verify a raw published directory under ``data_root``."""
    root_resolved = verify_data_root(data_root)
    directory_resolved = assert_path_under_root(root_resolved, directory)
    if not directory_resolved.is_dir():
        raise ManifestIntegrityError(
            "raw directory does not exist",
            context={"directory": str(directory)},
        )
    raw_bytes, manifest = _read_canonical_manifest(
        directory_resolved, expected_schema=RAW_MANIFEST_SCHEMA_VERSION
    )
    manifest_sha = _verify_manifest_self_sha(manifest)
    batch_id = manifest.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ManifestIntegrityError("manifest is missing a valid batch_id")
    _verify_complete_marker(
        directory_resolved,
        manifest=manifest,
        manifest_sha=manifest_sha,
        version_key="batch_id",
        expected_version=batch_id,
    )
    files = _verify_file_entries(
        manifest,
        directory_resolved,
        root_resolved=root_resolved,
        extra_managed=frozenset({RAW_INPUT_LISTING_FILENAME}),
        require_parquet_checks=False,
    )
    return VerifiedDirectory(
        manifest=manifest,
        manifest_bytes=raw_bytes,
        manifest_sha=manifest_sha,
        files=files,
    )


def verify_canonical_directory(directory: Path, *, data_root: Path) -> VerifiedDirectory:
    """Strictly verify a canonical published directory under ``data_root``."""
    root_resolved = verify_data_root(data_root)
    directory_resolved = assert_path_under_root(root_resolved, directory)
    if not directory_resolved.is_dir():
        raise ManifestIntegrityError(
            "canonical directory does not exist",
            context={"directory": str(directory)},
        )
    raw_bytes, manifest = _read_canonical_manifest(
        directory_resolved, expected_schema=WAREHOUSE_MANIFEST_SCHEMA_VERSION
    )
    manifest_sha = _verify_manifest_self_sha(manifest)
    version_id = manifest.get("version_id")
    if not isinstance(version_id, str) or not version_id.strip():
        raise ManifestIntegrityError("manifest is missing a valid version_id")
    _verify_complete_marker(
        directory_resolved,
        manifest=manifest,
        manifest_sha=manifest_sha,
        version_key="version_id",
        expected_version=version_id,
    )
    # Build the expected Parquet schema name list from the dataset contract.
    dataset = manifest.get("dataset")
    expected_schema: dict[str, object] | None = None
    if isinstance(dataset, str) and dataset:
        try:
            from quantking.data.adapters.canonical_schema import canonical_schema

            arrow_schema = canonical_schema(dataset)
            expected_schema = {"schema": arrow_schema}
        except KeyError:
            expected_schema = None
    files = _verify_file_entries(
        manifest,
        directory_resolved,
        root_resolved=root_resolved,
        extra_managed=frozenset({WAREHOUSE_INPUT_LISTING_FILENAME}),
        require_parquet_checks=True,
        expected_schema=expected_schema,
    )
    summary = manifest["coverage_summary"]
    if not isinstance(summary, dict):
        raise ManifestIntegrityError("manifest coverage_summary is invalid")
    declared_rows = int(str(summary["row_count"]))
    actual_rows = sum(file.row_count for file in files)
    if declared_rows != actual_rows:
        raise ManifestIntegrityError(
            "manifest total row_count does not match its Parquet files",
            context={"expected": str(declared_rows), "actual": str(actual_rows)},
        )
    return VerifiedDirectory(
        manifest=manifest,
        manifest_bytes=raw_bytes,
        manifest_sha=manifest_sha,
        files=files,
    )


__all__ = [
    "VerifiedDirectory",
    "VerifiedFile",
    "assert_path_under_root",
    "verify_canonical_directory",
    "verify_data_root",
    "verify_raw_directory",
]
