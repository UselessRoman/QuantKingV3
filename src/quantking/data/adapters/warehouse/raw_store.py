"""Raw batch landing implementation.

The raw store is the only component that physically writes supplier data
to disk. Its invariants are deliberately restrictive:

* Everything must land under the configured ``data_root`` and resolve to
  a relative path that is safe under :class:`InvalidPathError`.
* The on-disk layout is::

      <data_root>/raw/<source>/<dataset>/<batch_id>/<files...>

  The actual file writes happen inside an ``.incomplete`` sibling and
  only the final atomic rename promotes the directory to its real name.
* The deterministic manifest records source, dataset, batch id, adapter
  version, request parameter summary, creation time and the file
  inventory (managed relative path, size, row count and SHA-256).
* Successful publishes end with a ``COMPLETE`` marker whose contents
  bind the manifest SHA-256.
* A successful publish with the same inputs and manifest is a no-op and
  returns the same final directory. A successful publish with the same
  ``batch_id`` but different files, adapter version or request
  parameters raises :class:`IdempotencyConflictError`; the existing
  on-disk data is never overwritten.
* Any failure before the atomic rename leaves the ``.incomplete``
  directory, a ``failure.json`` and the input listing in place. A retry
  with the same ``batch_id`` is rejected (it is the responsibility of
  the caller to allocate a new ``batch_id`` for retry).

The module deliberately imports nothing from :mod:`quantking.data`
outside of the warehouse package; this keeps the data domain model
completely free of :mod:`pyarrow` and :mod:`duckdb`.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from quantking.common.errors import IdempotencyConflictError, InvalidPathError, InvalidTimeError
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.warehouse.errors import ManifestIntegrityError, RawStoreError
from quantking.data.adapters.warehouse.manifest import (
    RAW_COMPLETE_FILENAME,
    RAW_FAILURE_FILENAME,
    RAW_INPUT_LISTING_FILENAME,
    RAW_MANIFEST_FILENAME,
    RAW_MANIFEST_SCHEMA_VERSION,
    RawManifest,
    RawManifestFile,
    compute_sha256,
    load_raw_manifest,
    validate_managed_relative,
)
from quantking.data.adapters.warehouse.verifier import (
    assert_path_under_root,
    verify_data_root,
    verify_raw_directory,
)

# Characters that are never allowed in a single path component. This is
# deliberately stricter than ``_MANAGED_RE`` because source/dataset/batch_id
# become directory names and must never carry path separators, drive
# letters, namespace colons, control characters or whitespace.
_INVALID_COMPONENT_CHARS = set('<>"|?*\r\n\t\x00\\/:')
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def _validate_managed_component(value: str, field: str) -> str:
    """Validate that ``value`` is a single safe path component.

    A safe component is a non-empty string with no path separators
    (``/`` or ``\\``), no drive letters, no colons, no control
    characters, no leading or trailing whitespace, no ``.`` or ``..``
    relative references, and no reserved ``.incomplete`` suffix
    (case-insensitive).
    """
    if not isinstance(value, str):
        raise InvalidPathError(f"{field} must be a string")
    if not value or value != value.strip():
        raise InvalidPathError(f"{field} must not be empty or carry trailing whitespace")
    if any(ch in _INVALID_COMPONENT_CHARS or ord(ch) < 0x20 for ch in value):
        raise InvalidPathError(
            f"{field} must not contain path separators, colons or control characters"
        )
    if value in {".", ".."}:
        raise InvalidPathError(f"{field} must not be a relative reference")
    if value.lower().endswith(".incomplete"):
        raise InvalidPathError(f"{field} must not use the reserved .incomplete suffix")
    if not _SAFE_COMPONENT_RE.fullmatch(value):
        raise InvalidPathError(
            f"{field} must contain only ASCII letters, digits, dot, underscore or hyphen"
        )
    if value.endswith("."):
        raise InvalidPathError(f"{field} must not end with a dot")
    if value.split(".", maxsplit=1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise InvalidPathError(f"{field} must not use a reserved Windows device name")
    return value


@dataclass(frozen=True, slots=True)
class RawBatchPublishRequest:
    """The request to land one raw batch."""

    source: str
    dataset: str
    batch_id: str
    adapter_version: str
    request_params: Mapping[str, str]
    files: tuple[RawManifestFile, ...]
    created_at: str | None = None
    code_version: str = "stage1-raw-v1"

    def __post_init__(self) -> None:
        _validate_managed_component(self.source, "source")
        _validate_managed_component(self.dataset, "dataset")
        _validate_managed_component(self.batch_id, "batch_id")
        if not isinstance(self.adapter_version, str) or not self.adapter_version.strip():
            raise InvalidPathError("adapter_version must be non-empty text")
        if not isinstance(self.files, tuple):
            object.__setattr__(self, "files", tuple(self.files))
        normalised: dict[str, str] = {}
        for key, value in self.request_params.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("request_params must be a mapping of str to str")
            normalised[key] = value
        object.__setattr__(self, "request_params", dict(sorted(normalised.items())))
        if self.created_at is None:
            object.__setattr__(self, "created_at", _now_iso())


@dataclass(frozen=True, slots=True)
class RawBatchFailureRequest:
    """A managed Raw attempt that failed before a supplier payload existed."""

    source: str
    dataset: str
    batch_id: str
    adapter_version: str
    request_params: Mapping[str, str]
    error_code: str
    failed_at: str | None = None
    code_version: str = "stage1-raw-v1"

    def __post_init__(self) -> None:
        _validate_managed_component(self.source, "source")
        _validate_managed_component(self.dataset, "dataset")
        _validate_managed_component(self.batch_id, "batch_id")
        if not isinstance(self.adapter_version, str) or not self.adapter_version.strip():
            raise InvalidPathError("adapter_version must be non-empty text")
        if (
            not isinstance(self.error_code, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", self.error_code) is None
        ):
            raise InvalidPathError("error_code must be a stable uppercase identifier")
        normalized: dict[str, str] = {}
        for key, value in self.request_params.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("request_params must be a mapping of str to str")
            normalized[key] = value
        object.__setattr__(self, "request_params", dict(sorted(normalized.items())))
        if self.failed_at is None:
            object.__setattr__(self, "failed_at", _now_iso())
        else:
            try:
                timestamp = UtcTimestamp.from_iso(self.failed_at)
            except InvalidTimeError as error:
                raise InvalidPathError("failed_at must use the canonical UTC format") from error
            if timestamp.to_iso() != self.failed_at:
                raise InvalidPathError("failed_at must use the canonical UTC format")


def _now_iso() -> str:
    """Return a canonical UTC ISO-8601 timestamp with the ``Z`` suffix."""
    return _dt.datetime.now(_dt.UTC).isoformat().removesuffix("+00:00") + "Z"


class RawBatchPublisher:
    """Land raw batches under a managed data root.

    The publisher is constructed with a single absolute data root and
    refuses to operate outside it. The :meth:`publish` method is
    idempotent for identical successful inputs and raises
    :class:`IdempotencyConflictError` when an existing batch would be
    silently overwritten.
    """

    def __init__(self, *, data_root: Path) -> None:
        self._data_root = verify_data_root(data_root)

    # -- public API ---------------------------------------------------------

    def publish(self, request: RawBatchPublishRequest) -> Path:
        """Land the raw batch and return the final directory path.

        The directory lives at
        ``<data_root>/raw/<source>/<dataset>/<batch_id>/``. If the
        final directory already exists with an identical manifest the
        call is a no-op. If the final directory exists with a different
        manifest, :class:`IdempotencyConflictError` is raised.
        """
        self._validate_request(request)
        final = self._final_path(request)
        incomplete = self._incomplete_path(request)
        self._assert_managed_target(self._data_root, final)
        self._assert_managed_target(self._data_root, incomplete)
        if final.exists():
            return self._reconcile_existing(final, request)
        return self._publish_fresh(request, final)

    def record_failure(self, request: RawBatchFailureRequest) -> Path:
        """Persist an immutable managed failure attempt without a Raw payload."""
        if not isinstance(request, RawBatchFailureRequest):
            raise TypeError("request must be a RawBatchFailureRequest")
        surrogate = RawBatchPublishRequest(
            source=request.source,
            dataset=request.dataset,
            batch_id=request.batch_id,
            adapter_version=request.adapter_version,
            request_params=request.request_params,
            files=(),
            created_at=request.failed_at,
            code_version=request.code_version,
        )
        final = self._final_path(surrogate)
        incomplete = self._incomplete_path(surrogate)
        self._assert_managed_target(self._data_root, final)
        self._assert_managed_target(self._data_root, incomplete)
        if final.exists() or incomplete.exists():
            raise RawStoreError(
                "raw batch id already has immutable evidence",
                context={"batch_id": request.batch_id},
            )
        try:
            incomplete.mkdir(parents=True, exist_ok=False)
            self._write_input_listing(incomplete, surrogate)
            record = {
                "schema_version": "raw-attempt-failure.v1",
                "source": request.source,
                "dataset": request.dataset,
                "batch_id": request.batch_id,
                "adapter_version": request.adapter_version,
                "code_version": request.code_version,
                "error_code": request.error_code,
                "message": "raw attempt failed before supplier payload was available",
                "failed_at": request.failed_at,
            }
            target = incomplete / RAW_FAILURE_FILENAME
            with target.open("x", encoding="utf-8") as stream:
                json.dump(
                    record,
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                stream.flush()
                os.fsync(stream.fileno())
        except Exception as error:
            raise RawStoreError(
                "failed to preserve managed raw attempt evidence",
                context={"batch_id": request.batch_id},
            ) from error
        return incomplete

    # -- internals ----------------------------------------------------------

    def _validate_request(self, request: RawBatchPublishRequest) -> None:
        for entry in request.files:
            validate_managed_relative(entry.relative_path, field="relative_path")

    def _final_path(self, request: RawBatchPublishRequest) -> Path:
        return self._data_root / "raw" / request.source / request.dataset / request.batch_id

    def _incomplete_path(self, request: RawBatchPublishRequest) -> Path:
        final = self._final_path(request)
        return final.with_name(final.name + ".incomplete")

    def _publish_fresh(self, request: RawBatchPublishRequest, final: Path) -> Path:
        incomplete = self._incomplete_path(request)
        if incomplete.exists():
            raise RawStoreError(
                "incomplete directory already exists for this batch",
                context={"batch_id": request.batch_id},
            )
        try:
            incomplete.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise RawStoreError(
                "failed to create incomplete directory",
                context={"batch_id": request.batch_id},
            ) from error
        try:
            self._write_input_listing(incomplete, request)
            self._write_payloads(incomplete, request)
            manifest = self._build_manifest(request)
            self._write_manifest(incomplete, manifest)
            self._verify_inventory(incomplete, manifest)
            self._write_complete_marker(incomplete, manifest)
            self._assert_managed_target(self._data_root, incomplete)
            self._assert_managed_target(self._data_root, final)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(incomplete, final)
        except Exception as error:
            self._preserve_failure(incomplete, request, error)
            raise RawStoreError(
                "raw batch publish failed",
                context={"batch_id": request.batch_id},
            ) from error
        self._validate_final(final)
        return final

    def _reconcile_existing(self, final: Path, request: RawBatchPublishRequest) -> Path:
        # Run the unified strict verifier first. Any on-disk integrity
        # failure (tampered manifest, mutated file, missing COMPLETE)
        # surfaces as IdempotencyConflictError here because the caller is
        # trying to re-publish the same request against a directory that
        # no longer matches its declared state.
        try:
            verified = verify_raw_directory(final, data_root=self._data_root)
        except ManifestIntegrityError as error:
            raise IdempotencyConflictError(
                "existing raw directory failed integrity verification",
                context={"batch_id": request.batch_id},
            ) from error
        previous = verified.manifest
        if previous.get("manifest_schema_version") != RAW_MANIFEST_SCHEMA_VERSION:
            raise IdempotencyConflictError(
                "raw manifest schema version is unsupported",
                context={"batch_id": request.batch_id},
            )
        # Compare the on-disk manifest against the request's expected
        # manifest. The request's manifest is deterministic, so the
        # canonical JSON bytes must match exactly.
        current_manifest = self._build_manifest(request)
        current_json = current_manifest.to_canonical_json()
        previous_json_parts = {
            "manifest_schema_version": previous.get("manifest_schema_version"),
            "code_version": previous.get("code_version"),
            "source": previous.get("source"),
            "dataset": previous.get("dataset"),
            "batch_id": previous.get("batch_id"),
            "adapter_version": previous.get("adapter_version"),
            "created_at": previous.get("created_at"),
            "request_params": previous.get("request_params"),
            "files": previous.get("files"),
        }
        previous_json = json.dumps(
            previous_json_parts,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if previous_json != current_json:
            raise IdempotencyConflictError(
                "raw batch is already published with different inputs",
                context={"batch_id": request.batch_id},
            )
        return final

    def _write_payloads(self, directory: Path, request: RawBatchPublishRequest) -> None:
        for entry in request.files:
            target = directory / entry.relative_path
            self._assert_managed_target(directory, target)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._write_file(target, entry.payload)

    def _write_file(self, target: Path, payload: bytes) -> None:
        """Override this in tests to inject failures."""
        _default_write_file(target, payload)

    def _write_input_listing(self, directory: Path, request: RawBatchPublishRequest) -> None:
        payload = {
            "source": request.source,
            "dataset": request.dataset,
            "batch_id": request.batch_id,
            "adapter_version": request.adapter_version,
            "request_params": dict(request.request_params),
            "files": [
                {
                    "relative_path": entry.relative_path,
                    "size": len(entry.payload),
                    "row_count": entry.row_count,
                    "sha256": compute_sha256(entry.payload),
                }
                for entry in request.files
            ],
        }
        target = directory / RAW_INPUT_LISTING_FILENAME
        target.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

    def _build_manifest(self, request: RawBatchPublishRequest) -> RawManifest:
        return RawManifest(
            source=request.source,
            dataset=request.dataset,
            batch_id=request.batch_id,
            adapter_version=request.adapter_version,
            request_params=request.request_params,
            files=request.files,
            created_at=request.created_at or _now_iso(),
            code_version=request.code_version,
        )

    def _write_manifest(self, directory: Path, manifest: RawManifest) -> None:
        target = directory / RAW_MANIFEST_FILENAME
        # Write the signed manifest (with embedded manifest_sha256) so
        # the on-disk file is self-validating.
        target.write_text(manifest.to_signed_json(), encoding="utf-8")

    def _verify_inventory(self, directory: Path, manifest: RawManifest) -> None:
        # Pre-rename inventory check: verify every declared file is on
        # disk with the expected size and SHA. The unified verifier is
        # not used here because the manifest has not been promoted to
        # its final form yet (no COMPLETE marker at this stage).
        listed = {entry.relative_path: entry for entry in manifest.files}
        managed_set = {
            RAW_MANIFEST_FILENAME,
            RAW_INPUT_LISTING_FILENAME,
            RAW_COMPLETE_FILENAME,
            RAW_FAILURE_FILENAME,
        }
        for absolute in directory.rglob("*"):
            if absolute.is_dir():
                continue
            try:
                managed = absolute.relative_to(directory).as_posix()
            except ValueError as error:
                raise RawStoreError(
                    "raw file escapes its managed directory",
                ) from error
            if managed in managed_set:
                continue
            if managed not in listed:
                raise RawStoreError(
                    "raw directory has unexpected file",
                    context={"relative_path": managed},
                )
        for relative, entry in listed.items():
            absolute = directory / relative
            if not absolute.is_file():
                raise RawStoreError(
                    "raw manifest file is missing on disk",
                    context={"relative_path": relative},
                )
            payload = absolute.read_bytes()
            if len(payload) != len(entry.payload):
                raise RawStoreError(
                    "raw file size does not match the declared payload",
                    context={"relative_path": relative},
                )
            if compute_sha256(payload) != compute_sha256(entry.payload):
                raise RawStoreError(
                    "raw file SHA-256 does not match the declared payload",
                    context={"relative_path": relative},
                )

    def _write_complete_marker(self, directory: Path, manifest: RawManifest) -> None:
        target = directory / RAW_COMPLETE_FILENAME
        payload = {
            "batch_id": manifest.batch_id,
            "manifest_sha256": manifest.manifest_sha256(),
            "completed_at": _now_iso(),
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

    def _validate_final(self, final: Path) -> None:
        # Re-verify the final directory through the unified verifier so
        # the on-disk state is guaranteed consistent before the caller
        # treats the directory as published.
        try:
            verify_raw_directory(final, data_root=self._data_root)
        except ManifestIntegrityError as error:
            raise RawStoreError(
                "raw published directory failed integrity verification",
            ) from error

    def _assert_managed_target(self, root: Path, target: Path) -> None:
        # Delegate to the shared verifier helper so the symlink/junction
        # and containment checks are identical across Raw, Canonical and
        # Query.
        assert_path_under_root(self._data_root, target)

    def _preserve_failure(
        self, incomplete: Path, request: RawBatchPublishRequest, error: BaseException
    ) -> None:
        failure = _sanitize_failure_record(
            {
                "batch_id": request.batch_id,
                "source": request.source,
                "dataset": request.dataset,
                "adapter_version": request.adapter_version,
                "code_version": request.code_version,
                "failed_at": _now_iso(),
            },
            error=error,
        )
        if incomplete.exists():
            target = incomplete / RAW_FAILURE_FILENAME
            with contextlib.suppress(OSError):
                target.write_text(
                    json.dumps(
                        failure,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
            # The input listing is already on disk (written before any
            # payload), but we re-write a minimal listing if the failure
            # happened before _write_input_listing had a chance to run.
            input_listing_path = incomplete / RAW_INPUT_LISTING_FILENAME
            if not input_listing_path.is_file():
                with contextlib.suppress(OSError):
                    self._write_input_listing(incomplete, request)


def _sanitize_failure_record(
    record: dict[str, object], *, error: BaseException
) -> dict[str, object]:
    """Return a failure record with all sensitive content removed.

    The raw exception text and any absolute paths are stripped so the
    failure.json is safe to inspect without leaking filesystem layout,
    credentials or request keys. A stable ``error_category`` derived
    from the exception class replaces the raw message.
    """
    sanitized: dict[str, object] = {}
    for key, value in record.items():
        if isinstance(value, str):
            sanitized[key] = _sanitize_text(value)
        else:
            sanitized[key] = value
    sanitized["error_type"] = type(error).__name__
    sanitized["error_category"] = _classify_error(error)
    return sanitized


_ABSOLUTE_PATH_RE = re.compile(r"[A-Za-z]:[\\/]|^/")


def _sanitize_text(value: str) -> str:
    """Strip absolute paths and control characters from a string."""
    if not isinstance(value, str):
        return ""
    # Replace any absolute path pattern with a redaction marker.
    redacted = _ABSOLUTE_PATH_RE.sub("[REDACTED]", value)
    # Remove control characters except for the common whitespace chars.
    cleaned = "".join(ch if ch >= " " or ch in " \t" else "" for ch in redacted)
    return cleaned


def _classify_error(error: BaseException) -> str:
    """Return a stable, sanitized error category."""
    name = type(error).__name__
    if isinstance(error, OSError):
        return "IO_FAILURE"
    if isinstance(error, (KeyError, ValueError, TypeError)):
        return "CONTRACT_FAILURE"
    if isinstance(error, (RawStoreError, ManifestIntegrityError)):
        return "WAREHOUSE_FAILURE"
    return f"INTERNAL_FAILURE:{name}"


def _default_write_file(target: Path, payload: bytes) -> None:
    target.write_bytes(payload)


__all__ = [
    "RawBatchFailureRequest",
    "RawBatchPublishRequest",
    "RawBatchPublisher",
    "RawManifestFile",
    "load_raw_manifest",
]
