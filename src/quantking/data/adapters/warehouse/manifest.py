"""Deterministic manifest and COMPLETE-marker helpers for Raw and Warehouse.

The manifest is the single source of truth for what files belong to a
version. It is written as deterministic JSON (sorted keys, fixed
separators, no whitespace) so its SHA-256 is stable across platforms.

Raw and Canonical Warehouse share the same file conventions but produce
different payloads; the shared helpers live here, the per-side
serialisation helpers live in :mod:`quantking.data.adapters.warehouse.raw_store`
and :mod:`quantking.data.adapters.warehouse.canonical_warehouse`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from quantking.common.errors import InvalidPathError
from quantking.data.adapters.warehouse.errors import ManifestIntegrityError

RAW_MANIFEST_FILENAME = "manifest.json"
RAW_COMPLETE_FILENAME = "COMPLETE"
RAW_FAILURE_FILENAME = "failure.json"
RAW_INPUT_LISTING_FILENAME = "input_listing.json"

WAREHOUSE_MANIFEST_FILENAME = "manifest.json"
WAREHOUSE_COMPLETE_FILENAME = "COMPLETE"
WAREHOUSE_FAILURE_FILENAME = "failure.json"
WAREHOUSE_INPUT_LISTING_FILENAME = "input_listing.json"

RAW_MANIFEST_SCHEMA_VERSION = "raw-manifest.v1"
WAREHOUSE_MANIFEST_SCHEMA_VERSION = "warehouse-manifest.v1"

# A managed relative identifier: lowercase path with forward slashes,
# no traversal, no NUL bytes, no control characters. ``=`` is allowed
# because Hive-style partition keys (``year=2026``) are part of the
# canonical warehouse layout. ``:`` is intentionally NOT allowed
# because Windows file systems reject it and entity ids
# (``data_version:UUID4``) are mapped to a path-safe form by
# :func:`path_safe_id` before being used as a directory name.
_MANAGED_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.=/-]*\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def path_safe_id(value: str) -> str:
    """Convert an entity id (``namespace:UUID4``) to a path-safe form.

    The on-disk directory name for a :class:`quantking.common.ids.EntityId`
    is the namespace and UUID joined by ``--`` (double-dash). This
    preserves the round-trip through a simple split and is portable to
    Windows, macOS and Linux. Inputs that contain traversal segments,
    control characters or empty parts are rejected with
    :class:`InvalidPathError`.
    """
    if not isinstance(value, str) or not value.strip():
        raise InvalidPathError("identifier must be non-empty text")
    if "\x00" in value or any(ord(ch) < 0x20 for ch in value):
        raise InvalidPathError("identifier must not contain control characters")
    if ":" in value:
        namespace, _, raw_uuid = value.partition(":")
        if not namespace or not raw_uuid:
            raise InvalidPathError("identifier must use the form namespace:value")
        for part in (namespace, raw_uuid):
            if part in (".", "..") or "/" in part or "\\" in part:
                raise InvalidPathError(
                    "identifier parts must not be traversal segments or contain path separators"
                )
        return f"{namespace}--{raw_uuid}"
    # Bare identifier without a namespace separator; still must be safe.
    if value in (".", "..") or "/" in value or "\\" in value:
        raise InvalidPathError("identifier must not be a traversal segment or path")
    return value


def compute_sha256(payload: bytes) -> str:
    """Compute the lowercase hex SHA-256 of a byte payload."""
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be bytes")
    return hashlib.sha256(bytes(payload)).hexdigest()


def validate_managed_relative(value: str, *, field: str) -> str:
    """Validate that a string is a safe managed relative identifier."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidPathError(f"{field} must be non-empty text")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or len(candidate.drive) != 0:
        raise InvalidPathError(f"{field} must be a relative path, not absolute")
    if ".." in candidate.parts:
        raise InvalidPathError(f"{field} must not contain parent traversal")
    if not _MANAGED_RE.fullmatch(value):
        raise InvalidPathError(f"{field} must be a managed relative identifier")
    if "\x00" in value or any(ord(ch) < 0x20 for ch in value):
        raise InvalidPathError(f"{field} must not contain control characters")
    return value


@dataclass(frozen=True, slots=True)
class RawManifestFile:
    """One file entry in a raw batch manifest."""

    relative_path: str
    payload: bytes
    row_count: int

    def __post_init__(self) -> None:
        validate_managed_relative(self.relative_path, field="relative_path")
        if not isinstance(self.payload, (bytes, bytearray)):
            raise TypeError("payload must be bytes")
        if not isinstance(self.row_count, int) or isinstance(self.row_count, bool):
            raise TypeError("row_count must be an int")
        if self.row_count < 0:
            raise ValueError("row_count must be non-negative")
        object.__setattr__(self, "payload", bytes(self.payload))

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "size": len(self.payload),
            "sha256": compute_sha256(self.payload),
            "row_count": self.row_count,
        }


@dataclass(frozen=True, slots=True)
class RawManifest:
    """The deterministic manifest payload for one raw batch."""

    source: str
    dataset: str
    batch_id: str
    adapter_version: str
    request_params: Mapping[str, str]
    files: tuple[RawManifestFile, ...]
    created_at: str
    manifest_schema_version: str = RAW_MANIFEST_SCHEMA_VERSION
    code_version: str = "stage1-raw-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be non-empty text")
        if not isinstance(self.dataset, str) or not self.dataset.strip():
            raise ValueError("dataset must be non-empty text")
        if not isinstance(self.batch_id, str) or not self.batch_id.strip():
            raise ValueError("batch_id must be non-empty text")
        if not isinstance(self.adapter_version, str) or not self.adapter_version.strip():
            raise ValueError("adapter_version must be non-empty text")
        if not isinstance(self.created_at, str) or not self.created_at.strip():
            raise ValueError("created_at must be non-empty text")
        normalised: dict[str, str] = {}
        for key, value in self.request_params.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("request_params must be a mapping of str to str")
            normalised[key] = value
        object.__setattr__(
            self, "request_params", MappingProxyType(dict(sorted(normalised.items())))
        )
        normalised_files = tuple(sorted(self.files, key=lambda item: item.relative_path))
        object.__setattr__(self, "files", normalised_files)

    def to_canonical_json(self) -> str:
        payload = {
            "manifest_schema_version": self.manifest_schema_version,
            "code_version": self.code_version,
            "source": self.source,
            "dataset": self.dataset,
            "batch_id": self.batch_id,
            "adapter_version": self.adapter_version,
            "created_at": self.created_at,
            "request_params": dict(self.request_params),
            "files": [entry.to_dict() for entry in self.files],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def manifest_sha256(self) -> str:
        return compute_sha256(self.to_canonical_json().encode("utf-8"))

    def to_signed_json(self) -> str:
        """Return the canonical manifest JSON with ``manifest_sha256`` embedded.

        The SHA-256 is computed from :meth:`to_canonical_json` (which omits
        the field) and then injected into the output JSON so the on-disk
        file is self-validating. The unified published-directory verifier
        re-computes the hash from the canonical bytes and compares.
        """
        without_hash = self.to_canonical_json()
        digest = compute_sha256(without_hash.encode("utf-8"))
        payload = json.loads(without_hash)
        payload["manifest_sha256"] = digest
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class CanonicalManifestFile:
    """One file entry in a canonical warehouse manifest."""

    relative_path: str
    partition: Mapping[str, str]
    size: int
    row_count: int
    sha256: str

    def __post_init__(self) -> None:
        validate_managed_relative(self.relative_path, field="relative_path")
        normalised_partition: dict[str, str] = {}
        for key, value in self.partition.items():
            if not isinstance(key, str):
                raise TypeError("partition keys must be strings")
            # Partition values are stored as canonical strings at the
            # dataclass boundary (``year=2026`` is rendered as ``"2026"``);
            # the on-disk JSON serialiser in :meth:`to_dict` is free to
            # convert them back to integers for Hive-style readability.
            if not isinstance(value, str):
                raise TypeError("partition values must be strings")
            normalised_partition[key] = value
        object.__setattr__(
            self,
            "partition",
            MappingProxyType(dict(sorted(normalised_partition.items()))),
        )
        if not isinstance(self.size, int) or isinstance(self.size, bool) or self.size < 0:
            raise ValueError("size must be a non-negative integer")
        if not isinstance(self.row_count, int) or isinstance(self.row_count, bool):
            raise TypeError("row_count must be an int")
        if self.row_count < 0:
            raise ValueError("row_count must be non-negative")
        if not isinstance(self.sha256, str) or not _HEX64.fullmatch(self.sha256):
            raise ValueError("sha256 must be a 64-character lowercase hex string")

    def to_dict(self) -> dict[str, object]:
        # Try to convert numeric values back to int so the manifest on
        # disk keeps ``year=2026`` as a JSON integer rather than a string.
        partition: dict[str, object] = {}
        for key, value in self.partition.items():
            try:
                partition[key] = int(value)
            except (TypeError, ValueError):
                partition[key] = value
        return {
            "relative_path": self.relative_path,
            "partition": partition,
            "size": self.size,
            "row_count": self.row_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class CanonicalManifest:
    manifest_schema_version: str = WAREHOUSE_MANIFEST_SCHEMA_VERSION
    dataset: str = ""
    version_id: str = ""
    schema_version: str = ""
    code_version: str = ""
    rule_version: str = ""
    parent_version_id: str | None = None
    input_batch_ids: tuple[str, ...] = ()
    coverage_start: str | None = None
    coverage_end: str | None = None
    coverage_summary: Mapping[str, str] = field(default_factory=dict)
    files: tuple[CanonicalManifestFile, ...] = ()
    created_at: str = ""
    partition_scheme: Mapping[str, object] = field(default_factory=dict)
    content_sha256: str = ""
    source_override: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        for attr in ("dataset", "version_id", "schema_version", "code_version", "rule_version"):
            value = getattr(self, attr)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{attr} must be non-empty text")
        if not isinstance(self.created_at, str) or not self.created_at.strip():
            raise ValueError("created_at must be non-empty text")
        if self.parent_version_id is not None and not isinstance(self.parent_version_id, str):
            raise TypeError("parent_version_id must be a str or None")
        normalised_batch_ids = tuple(self.input_batch_ids)
        if len(normalised_batch_ids) != len(set(normalised_batch_ids)):
            raise ValueError("input_batch_ids must not contain duplicates")
        object.__setattr__(self, "input_batch_ids", normalised_batch_ids)
        normalised_summary: dict[str, str] = {}
        for key, value in self.coverage_summary.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("coverage_summary must map str to str")
            normalised_summary[key] = value
        object.__setattr__(
            self, "coverage_summary", MappingProxyType(dict(sorted(normalised_summary.items())))
        )
        object.__setattr__(
            self, "files", tuple(sorted(self.files, key=lambda item: item.relative_path))
        )
        partition: dict[str, object] = {}
        for key, value in self.partition_scheme.items():
            partition[key] = value
        object.__setattr__(
            self,
            "partition_scheme",
            MappingProxyType(dict(sorted(partition.items()))),
        )
        if self.source_override is not None:
            object.__setattr__(
                self,
                "source_override",
                MappingProxyType(dict(sorted(self.source_override.items()))),
            )

    def to_canonical_json(self) -> str:
        payload = {
            "content_sha256": self.content_sha256,
            "coverage_end": self.coverage_end,
            "coverage_start": self.coverage_start,
            "coverage_summary": dict(self.coverage_summary),
            "created_at": self.created_at,
            "dataset": self.dataset,
            "files": [entry.to_dict() for entry in self.files],
            "input_batch_ids": list(self.input_batch_ids),
            "manifest_schema_version": self.manifest_schema_version,
            "parent_version_id": self.parent_version_id,
            "partition_scheme": dict(self.partition_scheme),
            "rule_version": self.rule_version,
            "schema_version": self.schema_version,
            "code_version": self.code_version,
            "version_id": self.version_id,
        }
        if self.source_override is not None:
            payload["source_override"] = dict(self.source_override)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def manifest_sha256(self) -> str:
        """Return the canonical manifest SHA-256.

        The hash is computed from the canonical JSON serialisation *without*
        the ``manifest_sha256`` field so the value can be embedded in the
        file without creating a circular dependency. The COMPLETE marker
        then binds the same hash so the on-disk file is self-validating.
        """
        return compute_sha256(self.to_canonical_json().encode("utf-8"))

    def to_signed_json(self) -> str:
        """Return the canonical manifest JSON with ``manifest_sha256`` included.

        Used by on-disk readers that want a single signed artefact; the
        SHA-256 is computed from :meth:`to_canonical_json` (which omits the
        field) and then injected into the output JSON.
        """
        without_hash = self.to_canonical_json()
        digest = compute_sha256(without_hash.encode("utf-8"))
        payload = json.loads(without_hash)
        payload["manifest_sha256"] = digest
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def load_raw_manifest(directory: Path) -> dict[str, object]:
    """Load and validate the raw manifest JSON inside ``directory``."""
    from pathlib import Path

    path = Path(directory) / RAW_MANIFEST_FILENAME
    if not path.is_file():
        raise ManifestIntegrityError("raw manifest is missing", context={"path": str(path)})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ManifestIntegrityError(
            "raw manifest is not valid JSON",
            context={"path": str(path), "detail": str(error)},
        ) from error
    if not isinstance(data, dict):
        raise ManifestIntegrityError("raw manifest must be a JSON object")
    files = data.get("files")
    if not isinstance(files, list):
        raise ManifestIntegrityError("raw manifest 'files' must be a list")
    return data


def load_canonical_manifest(directory: Path) -> dict[str, object]:
    """Load and validate the canonical manifest JSON inside ``directory``."""
    from pathlib import Path

    path = Path(directory) / WAREHOUSE_MANIFEST_FILENAME
    if not path.is_file():
        raise ManifestIntegrityError("canonical manifest is missing", context={"path": str(path)})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ManifestIntegrityError(
            "canonical manifest is not valid JSON",
            context={"path": str(path), "detail": str(error)},
        ) from error
    if not isinstance(data, dict):
        raise ManifestIntegrityError("canonical manifest must be a JSON object")
    return data


__all__ = [
    "RAW_COMPLETE_FILENAME",
    "RAW_FAILURE_FILENAME",
    "RAW_INPUT_LISTING_FILENAME",
    "RAW_MANIFEST_FILENAME",
    "RAW_MANIFEST_SCHEMA_VERSION",
    "WAREHOUSE_COMPLETE_FILENAME",
    "WAREHOUSE_FAILURE_FILENAME",
    "WAREHOUSE_INPUT_LISTING_FILENAME",
    "WAREHOUSE_MANIFEST_FILENAME",
    "WAREHOUSE_MANIFEST_SCHEMA_VERSION",
    "CanonicalManifest",
    "CanonicalManifestFile",
    "RawManifest",
    "RawManifestFile",
    "compute_sha256",
    "load_canonical_manifest",
    "load_raw_manifest",
    "path_safe_id",
    "validate_managed_relative",
]
