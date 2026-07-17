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
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from quantking.common.errors import IdempotencyConflictError, InvalidPathError
from quantking.data.adapters.warehouse.errors import RawStoreError
from quantking.data.adapters.warehouse.manifest import (
    RAW_COMPLETE_FILENAME,
    RAW_FAILURE_FILENAME,
    RAW_INPUT_LISTING_FILENAME,
    RAW_MANIFEST_FILENAME,
    RawManifest,
    RawManifestFile,
    compute_sha256,
    load_raw_manifest,
    validate_managed_relative,
)

_INVALID_COMPONENT_CHARS = set('<>"|?*\r\n\t\x00')


def _validate_managed_component(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidPathError(f"{field} must be non-empty text")
    if any(ch in _INVALID_COMPONENT_CHARS for ch in value):
        raise InvalidPathError(f"{field} must not contain control characters")
    if value in {".", ".."}:
        raise InvalidPathError(f"{field} must not be a relative reference")
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
        root = Path(data_root)
        if not root.is_absolute():
            raise InvalidPathError("data_root must be an absolute path")
        if root.is_symlink() or _is_windows_junction(root):
            raise InvalidPathError(
                "data_root must not be a symlink or junction",
                context={"path": str(root)},
            )
        self._data_root = root.resolve()

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
        if final.exists():
            return self._reconcile_existing(final, request)
        return self._publish_fresh(request, final)

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
                context={"batch_id": request.batch_id, "detail": str(error)},
            ) from error
        try:
            self._write_input_listing(incomplete, request)
            self._write_payloads(incomplete, request)
            manifest = self._build_manifest(request)
            self._write_manifest(incomplete, manifest)
            self._verify_inventory(incomplete, manifest)
            self._write_complete_marker(incomplete, manifest)
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(incomplete, final)
        except Exception as error:
            self._preserve_failure(incomplete, request, error)
            if not incomplete.exists():
                # The atomic rename may have succeeded but the metadata
                # failed; remove the empty final directory so a retry
                # with the same batch id starts clean. The incomplete
                # directory is preserved for forensic review.
                self._safe_rmdir(final)
            raise RawStoreError(
                "raw batch publish failed",
                context={"batch_id": request.batch_id, "detail": str(error)},
            ) from error
        self._validate_final(final, manifest=manifest)
        return final

    def _reconcile_existing(self, final: Path, request: RawBatchPublishRequest) -> Path:
        manifest_path = final / RAW_MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise IdempotencyConflictError(
                "existing raw directory has no manifest",
                context={"batch_id": request.batch_id},
            )
        previous = load_raw_manifest(final)
        current = self._build_manifest(request).to_canonical_json()
        if previous.get("manifest_schema_version") != "raw-manifest.v1":
            raise IdempotencyConflictError(
                "raw manifest schema version is unsupported",
                context={"batch_id": request.batch_id},
            )
        existing_payload = json.dumps(
            {
                "manifest_schema_version": previous.get("manifest_schema_version"),
                "code_version": previous.get("code_version"),
                "source": previous.get("source"),
                "dataset": previous.get("dataset"),
                "batch_id": previous.get("batch_id"),
                "adapter_version": previous.get("adapter_version"),
                "created_at": previous.get("created_at"),
                "request_params": previous.get("request_params"),
                "files": previous.get("files"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if existing_payload != current:
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
        """Override this in tests to inject failures.

        The default implementation is :func:`_default_write_file`. We
        route through a method so subclasses can replace the behaviour
        without rebinding the instance attribute at construction time.
        """
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
        target.write_text(manifest.to_canonical_json(), encoding="utf-8")

    def _verify_inventory(self, directory: Path, manifest: RawManifest) -> None:
        listed = {entry.relative_path: entry for entry in manifest.files}
        on_disk: dict[str, tuple[int, str]] = {}
        for relative in listed:
            absolute = directory / relative
            if not absolute.is_file():
                raise RawStoreError(
                    "raw manifest file is missing on disk",
                    context={"relative_path": relative},
                )
            payload = absolute.read_bytes()
            on_disk[relative] = (len(payload), compute_sha256(payload))
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
                    context={"path": str(absolute)},
                ) from error
            if managed in managed_set:
                continue
            if managed not in listed:
                raise RawStoreError(
                    "raw directory has unexpected file",
                    context={"relative_path": managed},
                )
        for relative, entry in listed.items():
            size, sha = on_disk[relative]
            if size != len(entry.payload) or sha != compute_sha256(entry.payload):
                raise RawStoreError(
                    "raw file does not match declared payload",
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

    def _validate_final(self, final: Path, *, manifest: RawManifest) -> None:
        # Re-verify the inventory after the atomic rename so a corruption
        # between rename and marker write is caught before the directory
        # is treated as queryable.
        self._verify_inventory(final, manifest)
        marker = final / RAW_COMPLETE_FILENAME
        if not marker.is_file():
            raise RawStoreError(
                "raw COMPLETE marker is missing after publish",
                context={"batch_id": manifest.batch_id},
            )
        marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        expected = manifest.manifest_sha256()
        if marker_payload.get("manifest_sha256") != expected:
            raise RawStoreError(
                "raw COMPLETE marker does not match the manifest SHA-256",
                context={"batch_id": manifest.batch_id},
            )

    def _assert_managed_target(self, root: Path, target: Path) -> None:
        # Refuse to follow symlinks, junctions or parent escapes: every
        # raw directory must be a tree of regular files rooted inside
        # ``root``.
        try:
            resolved = target.resolve()
            root_resolved = root.resolve()
        except OSError as error:
            raise InvalidPathError(
                "raw target is not resolvable",
                context={"target": str(target), "detail": str(error)},
            ) from error
        if root_resolved != resolved and root_resolved not in resolved.parents:
            raise InvalidPathError(
                "raw target escapes the managed data root",
                context={"target": str(target), "root": str(root_resolved)},
            )
        # Refuse symlinks/junctions at the leaf and at every intermediate
        # level. The leaf check catches the common "fake file" attack;
        # the parent walk catches escape-via-junction at any depth.
        if target.is_symlink() or _is_windows_junction(target):
            raise InvalidPathError(
                "raw target is a symlink or junction",
                context={"target": str(target)},
            )
        for parent in resolved.parents:
            if parent == root_resolved:
                break
            if not parent.exists():
                continue
            if parent.is_symlink():
                raise InvalidPathError(
                    "raw target uses a symlink at an intermediate level",
                    context={"target": str(target), "intermediate": str(parent)},
                )
            if _is_windows_junction(parent):
                raise InvalidPathError(
                    "raw target uses a Windows junction",
                    context={"target": str(target), "intermediate": str(parent)},
                )

    def _preserve_failure(
        self, incomplete: Path, request: RawBatchPublishRequest, error: BaseException
    ) -> None:
        failure = {
            "batch_id": request.batch_id,
            "source": request.source,
            "dataset": request.dataset,
            "failed_at": _now_iso(),
            "error_type": type(error).__name__,
            "error_message": str(error),
        }
        if incomplete.exists():
            target = incomplete / RAW_FAILURE_FILENAME
            with contextlib.suppress(OSError):
                target.write_text(
                    json.dumps(failure, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                    encoding="utf-8",
                )

    def _safe_rmdir(self, path: Path) -> None:
        with contextlib.suppress(OSError):
            if path.is_dir():
                shutil.rmtree(path)


def _default_write_file(target: Path, payload: bytes) -> None:
    target.write_bytes(payload)


def _is_windows_junction(path: Path) -> bool:
    """Return True when ``path`` is a Windows symlink or directory junction.

    Python's :meth:`Path.is_symlink` only follows links whose target
    resolves; the constructor's :meth:`Path.symlink_to` on Windows can
    silently fail without raising. We therefore use the file attribute
    API to detect reparse points even when the symlink target cannot be
    resolved. Returns False on non-Windows platforms.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return path.is_symlink()


__all__ = [
    "RawBatchPublishRequest",
    "RawBatchPublisher",
    "RawManifestFile",
    "load_raw_manifest",
]
