"""S0-05 local snapshots, immutable object replication, and isolated restore."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path, PurePosixPath

from quantking.common.errors import InvalidTimeError
from quantking.common.time import UtcTimestamp
from quantking.jobs.backup.adapters.filesystem import (
    LocalStableFileCopier,
    SystemDiskSpace,
    sha256_file,
)
from quantking.jobs.backup.adapters.sqlite import SQLiteOnlineBackup
from quantking.jobs.backup.models import (
    ArtifactState,
    BackupCategory,
    BackupError,
    BackupFailureCode,
    FailureRecord,
    FileEntry,
    InputItem,
    InputManifest,
    LocalSnapshotRequest,
    LocalSnapshotResult,
    ReplicationReport,
    RestoreResult,
    SnapshotManifest,
    VerificationResult,
)
from quantking.jobs.backup.ports import DiskSpacePort, SQLiteBackupPort, StableFileCopierPort

_SCHEMA_VERSION = "1"
_DATABASE_PAYLOAD = "database/quantking.db"
_INPUT_FILE = "input.json"
_MANIFEST_FILE = "manifest.json"
_COMPLETE_FILE = "COMPLETE"
_FAILURE_FILE = "failure.json"
_VERIFIED_FILE = "VERIFIED"
_RESTORE_REQUEST_FILE = "restore-request.json"
_SOURCE_COMPLETE_FILE = "source-complete.json"
_SOURCE_REPLICATION_REPORT_FILE = "source-replication-report.json"
_REPLICATION_REPORT_FILE = "replication-report.json"
_LOCAL_SNAPSHOT_KIND = "local_snapshot"
_OFFSITE_RESTORE_POINT_KIND = "offsite_restore_point"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RestoreSource = tuple[
    SnapshotManifest,
    bytes,
    bytes,
    bytes,
    bytes | None,
    Callable[[FileEntry], Path],
]
_RestoreSourceLoader = Callable[[], _RestoreSource]


class BackupService:
    """One application service shared by CLI, tests, and future Worker handlers."""

    def __init__(
        self,
        *,
        disk_space: DiskSpacePort | None = None,
        file_copier: StableFileCopierPort | None = None,
        sqlite_backup: SQLiteBackupPort | None = None,
        before_snapshot_publish: Callable[[Path], None] | None = None,
        before_restore_verify: Callable[[Path], None] | None = None,
    ) -> None:
        self._disk_space = disk_space or SystemDiskSpace()
        self._file_copier = file_copier or LocalStableFileCopier()
        self._sqlite_backup = sqlite_backup or SQLiteOnlineBackup()
        self._before_snapshot_publish = before_snapshot_publish
        self._before_restore_verify = before_restore_verify

    def create_local_snapshot(self, request: LocalSnapshotRequest) -> LocalSnapshotResult:
        """Build in an incomplete directory and publish only after strict validation."""
        local_root, input_manifest, sources = _validate_local_request(request)
        final = local_root / request.run_id
        staging = local_root / f"{request.run_id}.incomplete"
        _require_new_targets(final, staging)
        local_root.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        input_bytes = _json_bytes(input_manifest.to_dict())
        try:
            _write_new(staging / _INPUT_FILE, input_bytes)
            required = _required_space_bytes(sources)
            if self._disk_space.free_bytes(local_root) < required:
                raise BackupError(
                    BackupFailureCode.INSUFFICIENT_SPACE,
                    "insufficient free space for bounded snapshot staging",
                )

            payload_root = staging / "payload"
            database_target = payload_root / _path_from_posix(_DATABASE_PAYLOAD)
            database_source = sources[_DATABASE_PAYLOAD]
            self._sqlite_backup.backup(database_source, database_target)
            database_entry = _entry_for_target(
                database_target,
                _DATABASE_PAYLOAD,
                BackupCategory.DATABASE,
            )
            entries = [database_entry]
            for item in input_manifest.inputs:
                if item.category is BackupCategory.DATABASE:
                    continue
                source = sources[item.payload_relative_path]
                target = payload_root / _path_from_posix(item.payload_relative_path)
                copied = self._file_copier.copy_stable(source, target)
                entries.append(
                    FileEntry(
                        relative_path=item.payload_relative_path,
                        size=copied.size,
                        sha256=copied.sha256,
                        category=item.category,
                    )
                )

            manifest = SnapshotManifest(
                schema_version=_SCHEMA_VERSION,
                restore_point_id=request.run_id,
                created_at=request.created_at,
                code_version=_required_text(request.code_version, "code version"),
                db_user_version=_required_non_negative_int(
                    request.db_user_version, "database user version"
                ),
                alembic_version=_required_text(request.alembic_version, "Alembic version"),
                input_manifest_sha256=_sha256_bytes(input_bytes),
                inputs=input_manifest.inputs,
                files=tuple(entries),
            )
            manifest_bytes = _json_bytes(manifest.to_dict())
            _write_new(staging / _MANIFEST_FILE, manifest_bytes)
            if self._before_snapshot_publish is not None:
                self._before_snapshot_publish(staging)
            _verify_local_tree(staging, require_complete=False)
            _write_new(
                staging / _COMPLETE_FILE,
                _completion_marker_bytes(
                    artifact_kind=_LOCAL_SNAPSHOT_KIND,
                    restore_point_id=request.run_id,
                    manifest_bytes=manifest_bytes,
                    input_bytes=input_bytes,
                ),
            )
            _verify_local_tree(staging, require_complete=True)
            _rename_new(staging, final)
            return LocalSnapshotResult(
                path=final,
                state=ArtifactState.COMPLETE,
                manifest=manifest,
            )
        except BackupError as error:
            _preserve_failure(staging, request.run_id, error, request.created_at)
            raise
        except Exception as error:
            converted = BackupError(
                BackupFailureCode.IO_FAILED,
                "local snapshot operation failed",
            )
            _preserve_failure(staging, request.run_id, converted, request.created_at)
            raise converted from error

    def replicate_to_offsite(
        self,
        snapshot: Path,
        offsite_root: Path,
        restore_point_id: str,
        *,
        created_at: UtcTimestamp,
    ) -> ReplicationReport:
        """Publish immutable SHA-256 objects and a new restore-point reference."""
        _validate_run_id(restore_point_id)
        snapshot_root = _absolute_existing_directory(snapshot, "snapshot")
        offsite = _absolute_root(offsite_root, "offsite root")
        _reject_overlap(snapshot_root, offsite)
        verification = verify_local_snapshot(snapshot_root)
        manifest = verification.manifest
        point_root = offsite / "restore_points"
        _validate_managed_path(offsite, point_root)
        final = point_root / restore_point_id
        staging = point_root / f"{restore_point_id}.incomplete"
        _validate_managed_path(offsite, final)
        _validate_managed_path(offsite, staging)
        _require_new_targets(final, staging)
        point_root.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        input_bytes = (snapshot_root / _INPUT_FILE).read_bytes()
        manifest_bytes = (snapshot_root / _MANIFEST_FILE).read_bytes()

        unique_entries = _unique_objects(manifest.files)
        copied = skipped = conflicted = 0
        try:
            _write_new(staging / _INPUT_FILE, input_bytes)
            _write_new(staging / _MANIFEST_FILE, manifest_bytes)
            missing_bytes = 0
            for digest, entry in unique_entries.items():
                target = object_path(offsite, digest)
                temporary = offsite / "objects" / ".incomplete" / restore_point_id / digest
                _validate_managed_path(offsite, target)
                _validate_managed_path(offsite, temporary)
                if target.exists():
                    _require_ordinary_file(target, "content-addressed object")
                else:
                    missing_bytes += entry.size
            if self._disk_space.free_bytes(offsite) < missing_bytes:
                raise BackupError(
                    BackupFailureCode.INSUFFICIENT_SPACE,
                    "insufficient free space for missing offsite objects",
                )

            for digest, entry in unique_entries.items():
                target = object_path(offsite, digest)
                _validate_managed_path(offsite, target)
                if target.exists():
                    _require_ordinary_file(target, "content-addressed object")
                    if _matches_entry(target, entry):
                        skipped += 1
                        continue
                    conflicted += 1
                    continue

                source = snapshot_root / "payload" / _path_from_posix(entry.relative_path)
                temporary = offsite / "objects" / ".incomplete" / restore_point_id / digest
                _validate_managed_path(offsite, temporary)
                result = self._file_copier.copy_stable(source, temporary)
                if result.size != entry.size or result.sha256 != entry.sha256:
                    raise BackupError(
                        BackupFailureCode.INTEGRITY_FAILED,
                        "staged content-addressed object failed manifest validation",
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    _require_ordinary_file(target, "content-addressed object")
                    if _matches_entry(target, entry):
                        skipped += 1
                        continue
                    conflicted += 1
                    continue
                _rename_new(temporary, target)
                copied += 1

            verify_local_snapshot(snapshot_root)
            if conflicted:
                raise BackupError(
                    BackupFailureCode.OBJECT_CONFLICT,
                    "one or more content-addressed objects conflicted with their digest",
                )
            report = ReplicationReport(
                schema_version=_SCHEMA_VERSION,
                restore_point_id=restore_point_id,
                created_at=created_at,
                total_objects=len(unique_entries),
                copied=copied,
                skipped=skipped,
                conflicted=conflicted,
                state=ArtifactState.COMPLETE,
            )
            report_bytes = _json_bytes(report.to_dict())
            _write_new(staging / _REPLICATION_REPORT_FILE, report_bytes)
            _verify_offsite_point(
                staging,
                offsite,
                expected_restore_point_id=restore_point_id,
                require_complete=False,
            )
            _write_new(
                staging / _COMPLETE_FILE,
                _completion_marker_bytes(
                    artifact_kind=_OFFSITE_RESTORE_POINT_KIND,
                    restore_point_id=restore_point_id,
                    source_restore_point_id=manifest.restore_point_id,
                    manifest_bytes=manifest_bytes,
                    input_bytes=input_bytes,
                    replication_report_bytes=report_bytes,
                ),
            )
            _verify_offsite_point(
                staging,
                offsite,
                expected_restore_point_id=restore_point_id,
                require_complete=True,
            )
            _rename_new(staging, final)
            return report
        except BackupError as error:
            report = ReplicationReport(
                schema_version=_SCHEMA_VERSION,
                restore_point_id=restore_point_id,
                created_at=created_at,
                total_objects=len(unique_entries),
                copied=copied,
                skipped=skipped,
                conflicted=conflicted,
                state=ArtifactState.FAILED,
            )
            _write_if_absent(staging / _REPLICATION_REPORT_FILE, _json_bytes(report.to_dict()))
            _preserve_failure(staging, restore_point_id, error, created_at)
            raise
        except Exception as error:
            converted = BackupError(
                BackupFailureCode.IO_FAILED,
                "offsite replication operation failed",
            )
            report = ReplicationReport(
                schema_version=_SCHEMA_VERSION,
                restore_point_id=restore_point_id,
                created_at=created_at,
                total_objects=len(unique_entries),
                copied=copied,
                skipped=skipped,
                conflicted=conflicted,
                state=ArtifactState.FAILED,
            )
            _write_if_absent(staging / _REPLICATION_REPORT_FILE, _json_bytes(report.to_dict()))
            _preserve_failure(staging, restore_point_id, converted, created_at)
            raise converted from error

    def restore_from_local(
        self,
        snapshot: Path,
        restore_root: Path,
        restore_id: str,
        *,
        verified_at: UtcTimestamp,
    ) -> RestoreResult:
        snapshot_root = _absolute_existing_directory(snapshot, "snapshot")
        return self._restore(
            source_root=snapshot_root,
            restore_root=restore_root,
            restore_id=restore_id,
            verified_at=verified_at,
            source_kind="local",
            source_restore_point_id=snapshot_root.name,
            load_source=lambda: _local_restore_source(snapshot_root),
        )

    def restore_from_offsite(
        self,
        offsite_root: Path,
        restore_point_id: str,
        restore_root: Path,
        restore_id: str,
        *,
        verified_at: UtcTimestamp,
    ) -> RestoreResult:
        _validate_run_id(restore_point_id)
        offsite = _absolute_existing_directory(offsite_root, "offsite root")
        return self._restore(
            source_root=offsite,
            restore_root=restore_root,
            restore_id=restore_id,
            verified_at=verified_at,
            source_kind="offsite",
            source_restore_point_id=restore_point_id,
            load_source=lambda: _offsite_restore_source(offsite, restore_point_id),
        )

    def _restore(
        self,
        *,
        source_root: Path,
        restore_root: Path,
        restore_id: str,
        verified_at: UtcTimestamp,
        source_kind: str,
        source_restore_point_id: str,
        load_source: _RestoreSourceLoader,
    ) -> RestoreResult:
        _validate_run_id(restore_id)
        destination_root = _absolute_root(restore_root, "restore root")
        _reject_overlap(source_root, destination_root)
        final = destination_root / restore_id
        staging = destination_root / f"{restore_id}.incomplete"
        _validate_managed_path(destination_root, final)
        _validate_managed_path(destination_root, staging)
        _require_new_targets(final, staging)
        destination_root.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        try:
            restore_request_bytes = _json_bytes(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "restore_id": restore_id,
                    "source_kind": source_kind,
                    "source_restore_point_id": source_restore_point_id,
                    "verified_at": verified_at.to_iso(),
                }
            )
            _write_new(staging / _RESTORE_REQUEST_FILE, restore_request_bytes)
            (
                manifest,
                input_bytes,
                manifest_bytes,
                complete_bytes,
                replication_report_bytes,
                resolver,
            ) = load_source()
            required = sum(entry.size for entry in manifest.files)
            if self._disk_space.free_bytes(destination_root) < required:
                raise BackupError(
                    BackupFailureCode.INSUFFICIENT_SPACE,
                    "insufficient free space for isolated restore payload",
                )
            _write_new(staging / _INPUT_FILE, input_bytes)
            _write_new(staging / _MANIFEST_FILE, manifest_bytes)
            _write_new(staging / _SOURCE_COMPLETE_FILE, complete_bytes)
            if replication_report_bytes is not None:
                _write_new(
                    staging / _SOURCE_REPLICATION_REPORT_FILE,
                    replication_report_bytes,
                )
            for entry in manifest.files:
                source = resolver(entry)
                target = staging / "payload" / _path_from_posix(entry.relative_path)
                _validate_managed_path(destination_root, target)
                copied = self._file_copier.copy_stable(source, target)
                if copied.size != entry.size or copied.sha256 != entry.sha256:
                    raise BackupError(
                        BackupFailureCode.INTEGRITY_FAILED,
                        "restored payload does not match its manifest entry",
                    )
            if self._before_restore_verify is not None:
                self._before_restore_verify(staging)
            _verify_materialized_restore(
                staging,
                require_verified=False,
                expected_restore_id=restore_id,
            )
            verified_marker: dict[str, object] = {
                "schema_version": _SCHEMA_VERSION,
                "restore_id": restore_id,
                "source_kind": source_kind,
                "source_restore_point_id": source_restore_point_id,
                "source_snapshot_id": manifest.restore_point_id,
                "verified_at": verified_at.to_iso(),
                "quick_check": "ok",
                "restore_request_sha256": _sha256_bytes(restore_request_bytes),
                "input_sha256": _sha256_bytes(input_bytes),
                "manifest_sha256": _sha256_bytes(manifest_bytes),
                "source_complete_sha256": _sha256_bytes(complete_bytes),
            }
            if replication_report_bytes is not None:
                verified_marker["source_replication_report_sha256"] = _sha256_bytes(
                    replication_report_bytes
                )
            _write_new(
                staging / _VERIFIED_FILE,
                _json_bytes(verified_marker),
            )
            _verify_materialized_restore(
                staging,
                require_verified=True,
                expected_restore_id=restore_id,
            )
            _rename_new(staging, final)
            return RestoreResult(path=final, state=ArtifactState.VERIFIED, manifest=manifest)
        except BackupError as error:
            _preserve_failure(staging, restore_id, error, verified_at)
            raise
        except Exception as error:
            converted = BackupError(
                BackupFailureCode.IO_FAILED,
                "isolated restore operation failed",
            )
            _preserve_failure(staging, restore_id, converted, verified_at)
            raise converted from error


def verify_local_snapshot(snapshot: Path) -> VerificationResult:
    root = _absolute_existing_directory(snapshot, "snapshot")
    if root.name.endswith(".incomplete"):
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "incomplete directories are not published recovery points",
        )
    manifest = _verify_local_tree(root, require_complete=True)
    return VerificationResult(state=ArtifactState.COMPLETE, manifest=manifest)


def verify_materialized_restore(restore: Path) -> VerificationResult:
    """Re-verify one published isolated restore and every bound metadata document."""
    root = _absolute_existing_directory(restore, "materialized restore")
    if root.name.endswith(".incomplete"):
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "incomplete directories are not published verified restores",
        )
    manifest = _verify_materialized_restore(
        root,
        require_verified=True,
        expected_restore_id=root.name,
    )
    return VerificationResult(state=ArtifactState.VERIFIED, manifest=manifest)


def load_snapshot_manifest(snapshot: Path) -> SnapshotManifest:
    return verify_local_snapshot(snapshot).manifest


def object_path(offsite_root: Path, digest: str) -> Path:
    if not isinstance(digest, str) or _HEX_DIGEST.fullmatch(digest) is None:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "content-addressed object digest is invalid",
        )
    return offsite_root / "objects" / "sha256" / digest[:2] / digest


def _validate_local_request(
    request: LocalSnapshotRequest,
) -> tuple[Path, InputManifest, dict[str, Path]]:
    _validate_run_id(request.run_id)
    source_root = _absolute_existing_directory(request.source_root, "source root")
    local_root = _absolute_root(request.local_backup_root, "local backup root")
    _reject_overlap(source_root, local_root)
    database_relative = _validate_posix_relative(request.database_relative_path)
    database_source = _resolve_existing_file(source_root, database_relative)
    inputs = [
        InputItem(
            source_relative_path=database_relative,
            payload_relative_path=_DATABASE_PAYLOAD,
            category=BackupCategory.DATABASE,
        )
    ]
    sources: dict[str, Path] = {_DATABASE_PAYLOAD: database_source}
    for spec in request.files:
        if (
            not isinstance(spec.category, BackupCategory)
            or spec.category is BackupCategory.DATABASE
        ):
            raise BackupError(
                BackupFailureCode.MANIFEST_INVALID,
                "ordinary source category is invalid",
            )
        relative = _validate_posix_relative(spec.relative_path)
        if relative == _DATABASE_PAYLOAD or relative in sources:
            raise BackupError(
                BackupFailureCode.MANIFEST_INVALID,
                "source payload paths must be unique and cannot shadow the database",
            )
        source = _resolve_existing_file(source_root, relative)
        inputs.append(
            InputItem(
                source_relative_path=relative,
                payload_relative_path=relative,
                category=spec.category,
            )
        )
        sources[relative] = source
    return (
        local_root,
        InputManifest(schema_version=_SCHEMA_VERSION, run_id=request.run_id, inputs=tuple(inputs)),
        sources,
    )


def _verify_local_tree(root: Path, *, require_complete: bool) -> SnapshotManifest:
    manifest_bytes = _read_bytes(root / _MANIFEST_FILE, BackupFailureCode.MANIFEST_INVALID)
    input_bytes = _read_bytes(root / _INPUT_FILE, BackupFailureCode.MANIFEST_INVALID)
    manifest = _parse_manifest(manifest_bytes)
    input_manifest = _parse_input_manifest(input_bytes)
    _validate_snapshot_metadata(manifest, input_manifest, input_bytes)
    expected = {_INPUT_FILE, _MANIFEST_FILE}
    expected.update(f"payload/{entry.relative_path}" for entry in manifest.files)
    if require_complete:
        expected.add(_COMPLETE_FILE)
        _verify_complete_marker(
            root / _COMPLETE_FILE,
            artifact_kind=_LOCAL_SNAPSHOT_KIND,
            expected_restore_point_id=manifest.restore_point_id,
            manifest_bytes=manifest_bytes,
            input_bytes=input_bytes,
        )
    _verify_exact_file_set(root, expected)
    _verify_entries(root / "payload", manifest)
    _verify_sqlite_payload(root / "payload", manifest)
    _verify_exact_file_set(root, expected)
    return manifest


def _verify_materialized_restore(
    root: Path,
    *,
    require_verified: bool,
    expected_restore_id: str,
) -> SnapshotManifest:
    restore_request_bytes = _read_bytes(
        root / _RESTORE_REQUEST_FILE, BackupFailureCode.MANIFEST_INVALID
    )
    manifest_bytes = _read_bytes(root / _MANIFEST_FILE, BackupFailureCode.MANIFEST_INVALID)
    input_bytes = _read_bytes(root / _INPUT_FILE, BackupFailureCode.MANIFEST_INVALID)
    manifest = _parse_manifest(manifest_bytes)
    input_manifest = _parse_input_manifest(input_bytes)
    _validate_snapshot_metadata(manifest, input_manifest, input_bytes)
    request = _parse_restore_request(restore_request_bytes)
    if request["restore_id"] != expected_restore_id:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "restore request ID does not match its directory",
        )
    source_kind = request["source_kind"]
    source_restore_point_id = request["source_restore_point_id"]
    source_complete = _read_bytes(root / _SOURCE_COMPLETE_FILE, BackupFailureCode.MANIFEST_INVALID)
    expected = {
        _RESTORE_REQUEST_FILE,
        _INPUT_FILE,
        _MANIFEST_FILE,
        _SOURCE_COMPLETE_FILE,
    }
    replication_report_bytes: bytes | None = None
    if source_kind == "local":
        if source_restore_point_id != manifest.restore_point_id:
            raise BackupError(
                BackupFailureCode.MANIFEST_INVALID,
                "local source ID does not match the snapshot manifest",
            )
        _verify_complete_bytes(
            source_complete,
            artifact_kind=_LOCAL_SNAPSHOT_KIND,
            expected_restore_point_id=source_restore_point_id,
            manifest_bytes=manifest_bytes,
            input_bytes=input_bytes,
        )
    elif source_kind == "offsite":
        replication_report_bytes = _read_bytes(
            root / _SOURCE_REPLICATION_REPORT_FILE,
            BackupFailureCode.MANIFEST_INVALID,
        )
        expected.add(_SOURCE_REPLICATION_REPORT_FILE)
        report = _parse_replication_report(replication_report_bytes)
        _validate_complete_replication_report(
            report,
            expected_restore_point_id=source_restore_point_id,
            manifest=manifest,
        )
        _verify_complete_bytes(
            source_complete,
            artifact_kind=_OFFSITE_RESTORE_POINT_KIND,
            expected_restore_point_id=source_restore_point_id,
            source_restore_point_id=manifest.restore_point_id,
            manifest_bytes=manifest_bytes,
            input_bytes=input_bytes,
            replication_report_bytes=replication_report_bytes,
        )
    else:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "restore source kind is unsupported",
        )
    expected.update(f"payload/{entry.relative_path}" for entry in manifest.files)
    if require_verified:
        expected.add(_VERIFIED_FILE)
        verified_bytes = _read_bytes(root / _VERIFIED_FILE, BackupFailureCode.MANIFEST_INVALID)
        _verify_verified_marker(
            verified_bytes,
            restore_request=request,
            expected_restore_id=expected_restore_id,
            manifest=manifest,
            restore_request_bytes=restore_request_bytes,
            input_bytes=input_bytes,
            manifest_bytes=manifest_bytes,
            source_complete_bytes=source_complete,
            replication_report_bytes=replication_report_bytes,
        )
    _verify_exact_file_set(root, expected)
    _verify_entries(root / "payload", manifest)
    _verify_sqlite_payload(root / "payload", manifest)
    _verify_exact_file_set(root, expected)
    return manifest


def _verify_offsite_point(
    point: Path,
    offsite_root: Path,
    *,
    expected_restore_point_id: str,
    require_complete: bool,
) -> SnapshotManifest:
    _validate_managed_path(offsite_root, point)
    if _is_reparse_point(point):
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            "offsite restore point cannot be a reparse point",
        )
    manifest_bytes = _read_bytes(point / _MANIFEST_FILE, BackupFailureCode.MANIFEST_INVALID)
    input_bytes = _read_bytes(point / _INPUT_FILE, BackupFailureCode.MANIFEST_INVALID)
    report_bytes = _read_bytes(point / _REPLICATION_REPORT_FILE, BackupFailureCode.MANIFEST_INVALID)
    manifest = _parse_manifest(manifest_bytes)
    input_manifest = _parse_input_manifest(input_bytes)
    _validate_snapshot_metadata(manifest, input_manifest, input_bytes)
    report = _parse_replication_report(report_bytes)
    _validate_complete_replication_report(
        report,
        expected_restore_point_id=expected_restore_point_id,
        manifest=manifest,
    )
    expected = {_INPUT_FILE, _MANIFEST_FILE, _REPLICATION_REPORT_FILE}
    if require_complete:
        expected.add(_COMPLETE_FILE)
        _verify_complete_marker(
            point / _COMPLETE_FILE,
            artifact_kind=_OFFSITE_RESTORE_POINT_KIND,
            expected_restore_point_id=expected_restore_point_id,
            source_restore_point_id=manifest.restore_point_id,
            manifest_bytes=manifest_bytes,
            input_bytes=input_bytes,
            replication_report_bytes=report_bytes,
        )
    _verify_exact_file_set(point, expected)
    for entry in _unique_objects(manifest.files).values():
        candidate = object_path(offsite_root, entry.sha256)
        _validate_managed_path(offsite_root, candidate)
        _require_ordinary_file(candidate, "content-addressed object")
        if not _matches_entry(candidate, entry):
            raise BackupError(
                BackupFailureCode.INTEGRITY_FAILED,
                "offsite object is missing or does not match its manifest",
            )
    return manifest


def _local_restore_source(
    snapshot: Path,
) -> _RestoreSource:
    manifest = verify_local_snapshot(snapshot).manifest
    return (
        manifest,
        (snapshot / _INPUT_FILE).read_bytes(),
        (snapshot / _MANIFEST_FILE).read_bytes(),
        (snapshot / _COMPLETE_FILE).read_bytes(),
        None,
        lambda entry: snapshot / "payload" / _path_from_posix(entry.relative_path),
    )


def _offsite_restore_source(offsite: Path, restore_point_id: str) -> _RestoreSource:
    point = offsite / "restore_points" / restore_point_id
    _validate_managed_path(offsite, point)
    if not point.is_dir():
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "offsite restore point does not exist",
        )
    manifest = _verify_offsite_point(
        point,
        offsite,
        expected_restore_point_id=restore_point_id,
        require_complete=True,
    )
    return (
        manifest,
        (point / _INPUT_FILE).read_bytes(),
        (point / _MANIFEST_FILE).read_bytes(),
        (point / _COMPLETE_FILE).read_bytes(),
        (point / _REPLICATION_REPORT_FILE).read_bytes(),
        lambda entry: _validated_offsite_object(offsite, entry),
    )


def _verify_entries(payload_root: Path, manifest: SnapshotManifest) -> None:
    for entry in manifest.files:
        _validate_posix_relative(entry.relative_path)
        if entry.size < 0 or _HEX_DIGEST.fullmatch(entry.sha256) is None:
            raise BackupError(
                BackupFailureCode.MANIFEST_INVALID,
                "manifest file size or digest is invalid",
            )
        path = payload_root / _path_from_posix(entry.relative_path)
        resolved = path.resolve(strict=False)
        if not _is_descendant(resolved, payload_root.resolve(strict=False)) or not path.is_file():
            raise BackupError(
                BackupFailureCode.INTEGRITY_FAILED,
                "manifest payload file is missing or escaped its root",
            )
        if (
            path.is_symlink()
            or path.stat().st_size != entry.size
            or sha256_file(path) != entry.sha256
        ):
            raise BackupError(
                BackupFailureCode.INTEGRITY_FAILED,
                "manifest payload file failed size or checksum validation",
            )


def _verify_sqlite_payload(payload_root: Path, manifest: SnapshotManifest) -> None:
    database_entries = [
        entry for entry in manifest.files if entry.category is BackupCategory.DATABASE
    ]
    if len(database_entries) != 1 or database_entries[0].relative_path != _DATABASE_PAYLOAD:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "manifest must contain exactly one canonical database payload",
        )
    database = payload_root / _path_from_posix(_DATABASE_PAYLOAD)
    sidecars = (Path(f"{database}-wal"), Path(f"{database}-shm"))
    if any(path.exists() for path in sidecars):
        raise BackupError(
            BackupFailureCode.PAYLOAD_MISMATCH,
            "completed database payload contains a WAL or SHM sidecar",
        )
    uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            user_version_row = connection.execute("PRAGMA user_version").fetchone()
            alembic_rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.Error as error:
        raise BackupError(
            BackupFailureCode.INTEGRITY_FAILED,
            "immutable SQLite validation failed",
        ) from error
    if quick_check != ("ok",):
        raise BackupError(
            BackupFailureCode.INTEGRITY_FAILED,
            "immutable SQLite quick_check failed",
        )
    if user_version_row != (manifest.db_user_version,) or alembic_rows != [
        (manifest.alembic_version,)
    ]:
        raise BackupError(
            BackupFailureCode.SCHEMA_MISMATCH,
            "SQLite schema or Alembic version does not match the manifest",
        )
    if any(path.exists() for path in sidecars):
        raise BackupError(
            BackupFailureCode.PAYLOAD_MISMATCH,
            "immutable SQLite validation created a forbidden sidecar",
        )


def _verify_exact_file_set(root: Path, expected: set[str]) -> None:
    actual: set[str] = set()
    try:
        for path in root.rglob("*"):
            if _is_reparse_point(path):
                raise BackupError(
                    BackupFailureCode.INVALID_PATH,
                    "backup tree contains a symbolic link or junction escape",
                )
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
    except OSError as error:
        raise BackupError(
            BackupFailureCode.IO_FAILED,
            "backup file set could not be inspected",
        ) from error
    if actual != expected:
        raise BackupError(
            BackupFailureCode.PAYLOAD_MISMATCH,
            "actual backup file set does not exactly match the manifest",
        )


def _completion_marker_bytes(
    *,
    artifact_kind: str,
    restore_point_id: str,
    manifest_bytes: bytes,
    input_bytes: bytes,
    source_restore_point_id: str | None = None,
    replication_report_bytes: bytes | None = None,
) -> bytes:
    marker: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "artifact_kind": artifact_kind,
        "restore_point_id": restore_point_id,
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "input_sha256": _sha256_bytes(input_bytes),
    }
    if artifact_kind == _OFFSITE_RESTORE_POINT_KIND:
        if source_restore_point_id is None or replication_report_bytes is None:
            raise ValueError("offsite completion marker requires source ID and report")
        marker["source_restore_point_id"] = source_restore_point_id
        marker["replication_report_sha256"] = _sha256_bytes(replication_report_bytes)
    elif artifact_kind != _LOCAL_SNAPSHOT_KIND:
        raise ValueError("unsupported completion marker kind")
    return _json_bytes(marker)


def _verify_complete_marker(
    path: Path,
    *,
    artifact_kind: str,
    expected_restore_point_id: str,
    manifest_bytes: bytes,
    input_bytes: bytes,
    source_restore_point_id: str | None = None,
    replication_report_bytes: bytes | None = None,
) -> None:
    _verify_complete_bytes(
        _read_bytes(path, BackupFailureCode.MANIFEST_INVALID),
        artifact_kind=artifact_kind,
        expected_restore_point_id=expected_restore_point_id,
        manifest_bytes=manifest_bytes,
        input_bytes=input_bytes,
        source_restore_point_id=source_restore_point_id,
        replication_report_bytes=replication_report_bytes,
    )


def _verify_complete_bytes(
    marker_bytes: bytes,
    *,
    artifact_kind: str,
    expected_restore_point_id: str,
    manifest_bytes: bytes,
    input_bytes: bytes,
    source_restore_point_id: str | None = None,
    replication_report_bytes: bytes | None = None,
) -> None:
    expected = json.loads(
        _completion_marker_bytes(
            artifact_kind=artifact_kind,
            restore_point_id=expected_restore_point_id,
            manifest_bytes=manifest_bytes,
            input_bytes=input_bytes,
            source_restore_point_id=source_restore_point_id,
            replication_report_bytes=replication_report_bytes,
        )
    )
    raw = _parse_strict_mapping(
        marker_bytes,
        expected_fields=set(expected),
        document="completion marker",
    )
    if raw != expected:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "completion marker does not bind the expected metadata and IDs",
        )


def _validate_snapshot_metadata(
    manifest: SnapshotManifest,
    input_manifest: InputManifest,
    input_bytes: bytes,
) -> None:
    if (
        manifest.schema_version != _SCHEMA_VERSION
        or input_manifest.schema_version != _SCHEMA_VERSION
    ):
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "backup manifest schema version is unsupported",
        )
    if (
        manifest.restore_point_id != input_manifest.run_id
        or manifest.inputs != input_manifest.inputs
    ):
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "input manifest does not match snapshot manifest",
        )
    if manifest.input_manifest_sha256 != _sha256_bytes(input_bytes):
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "input manifest checksum does not match snapshot manifest",
        )


def _parse_replication_report(payload: bytes) -> ReplicationReport:
    try:
        raw = json.loads(payload)
        if not isinstance(raw, Mapping):
            raise ValueError("replication report must be an object")
        return ReplicationReport.from_dict(raw)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        InvalidTimeError,
        TypeError,
        ValueError,
    ) as error:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "replication report is invalid",
        ) from error


def _validate_complete_replication_report(
    report: ReplicationReport,
    *,
    expected_restore_point_id: str,
    manifest: SnapshotManifest,
) -> None:
    total = len(_unique_objects(manifest.files))
    if (
        report.schema_version != _SCHEMA_VERSION
        or report.restore_point_id != expected_restore_point_id
        or report.state is not ArtifactState.COMPLETE
        or report.total_objects != total
        or report.copied + report.skipped + report.conflicted != report.total_objects
        or report.conflicted != 0
    ):
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "completed replication report is inconsistent with its point and manifest",
        )


def _parse_restore_request(payload: bytes) -> dict[str, str]:
    raw = _parse_strict_mapping(
        payload,
        expected_fields={
            "schema_version",
            "restore_id",
            "source_kind",
            "source_restore_point_id",
            "verified_at",
        },
        document="restore request",
    )
    values: dict[str, str] = {}
    for key in raw:
        value = raw[key]
        if not isinstance(value, str) or not value:
            raise BackupError(
                BackupFailureCode.MANIFEST_INVALID,
                "restore request fields must be non-empty text",
            )
        values[key] = value
    if values["schema_version"] != _SCHEMA_VERSION:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "restore request schema version is unsupported",
        )
    try:
        timestamp = UtcTimestamp.from_iso(values["verified_at"])
    except (InvalidTimeError, TypeError, ValueError) as error:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "restore request timestamp is invalid",
        ) from error
    if timestamp.to_iso() != values["verified_at"]:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "restore request timestamp is not canonical UTC",
        )
    return values


def _verify_verified_marker(
    payload: bytes,
    *,
    restore_request: Mapping[str, str],
    expected_restore_id: str,
    manifest: SnapshotManifest,
    restore_request_bytes: bytes,
    input_bytes: bytes,
    manifest_bytes: bytes,
    source_complete_bytes: bytes,
    replication_report_bytes: bytes | None,
) -> None:
    expected: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "restore_id": expected_restore_id,
        "source_kind": restore_request["source_kind"],
        "source_restore_point_id": restore_request["source_restore_point_id"],
        "source_snapshot_id": manifest.restore_point_id,
        "verified_at": restore_request["verified_at"],
        "quick_check": "ok",
        "restore_request_sha256": _sha256_bytes(restore_request_bytes),
        "input_sha256": _sha256_bytes(input_bytes),
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "source_complete_sha256": _sha256_bytes(source_complete_bytes),
    }
    if replication_report_bytes is not None:
        expected["source_replication_report_sha256"] = _sha256_bytes(replication_report_bytes)
    raw = _parse_strict_mapping(
        payload,
        expected_fields=set(expected),
        document="verified marker",
    )
    if raw != expected:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "verified marker does not bind the restore metadata and IDs",
        )


def _parse_strict_mapping(
    payload: bytes,
    *,
    expected_fields: set[str],
    document: str,
) -> Mapping[str, object]:
    try:
        raw = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            f"{document} is not valid JSON",
        ) from error
    if not isinstance(raw, Mapping) or set(raw) != expected_fields:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            f"{document} fields do not match the supported schema",
        )
    return raw


def _parse_manifest(payload: bytes) -> SnapshotManifest:
    try:
        raw = json.loads(payload)
        if not isinstance(raw, Mapping):
            raise ValueError("manifest must be an object")
        return SnapshotManifest.from_dict(raw)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        InvalidTimeError,
        TypeError,
        ValueError,
    ) as error:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "snapshot manifest is invalid",
        ) from error


def _parse_input_manifest(payload: bytes) -> InputManifest:
    try:
        raw = json.loads(payload)
        if not isinstance(raw, Mapping):
            raise ValueError("input manifest must be an object")
        return InputManifest.from_dict(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as error:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            "input manifest is invalid",
        ) from error


def _entry_for_target(path: Path, relative: str, category: BackupCategory) -> FileEntry:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise BackupError(
            BackupFailureCode.IO_FAILED,
            "payload metadata could not be read",
        ) from error
    return FileEntry(relative_path=relative, size=size, sha256=sha256_file(path), category=category)


def _matches_entry(path: Path, entry: FileEntry) -> bool:
    try:
        return (
            path.is_file()
            and path.stat().st_size == entry.size
            and sha256_file(path) == entry.sha256
        )
    except BackupError:
        return False
    except OSError:
        return False


def _validated_offsite_object(offsite_root: Path, entry: FileEntry) -> Path:
    candidate = object_path(offsite_root, entry.sha256)
    _validate_managed_path(offsite_root, candidate)
    _require_ordinary_file(candidate, "content-addressed object")
    return candidate


def _unique_objects(entries: tuple[FileEntry, ...]) -> dict[str, FileEntry]:
    unique: dict[str, FileEntry] = {}
    for entry in entries:
        existing = unique.get(entry.sha256)
        if existing is not None and existing.size != entry.size:
            raise BackupError(
                BackupFailureCode.MANIFEST_INVALID,
                "equal manifest digests cannot declare different sizes",
            )
        unique.setdefault(entry.sha256, entry)
    return dict(sorted(unique.items()))


def _required_space_bytes(sources: dict[str, Path]) -> int:
    total = 0
    try:
        for path in sources.values():
            total += path.stat().st_size
        database_source = sources[_DATABASE_PAYLOAD]
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database_source}{suffix}")
            if sidecar.exists():
                total += sidecar.stat().st_size
    except OSError as error:
        raise BackupError(
            BackupFailureCode.IO_FAILED,
            "source size could not be determined",
        ) from error
    return max(total * 2, total + 1024 * 1024)


def _validate_run_id(run_id: str) -> None:
    if (
        not isinstance(run_id, str)
        or _RUN_ID.fullmatch(run_id) is None
        or run_id.endswith(".incomplete")
    ):
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            "artifact ID is not a safe unique directory name",
        )


def _validate_posix_relative(relative: str) -> str:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            "manifest paths must be non-empty POSIX relative paths",
        )
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in pure.parts):
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            "manifest path escapes its managed root",
        )
    normalized = pure.as_posix()
    if normalized != relative:
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            "manifest path must use canonical POSIX separators",
        )
    return normalized


def _path_from_posix(relative: str) -> Path:
    pure = PurePosixPath(_validate_posix_relative(relative))
    return Path(*pure.parts)


def _absolute_existing_directory(path: Path, field: str) -> Path:
    root = _absolute_root(path, field)
    if not root.is_dir() or root.is_symlink():
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            f"{field} must be an existing ordinary directory",
        )
    return root


def _absolute_root(path: Path, field: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            f"{field} must be an absolute managed path",
        )
    try:
        if _is_reparse_point(path):
            raise BackupError(
                BackupFailureCode.INVALID_PATH,
                f"{field} cannot be a symbolic link or junction",
            )
        return path.resolve(strict=False)
    except BackupError:
        raise
    except (OSError, RuntimeError) as error:
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            f"{field} could not be resolved safely",
        ) from error


def _resolve_existing_file(root: Path, relative: str) -> Path:
    candidate = root / _path_from_posix(relative)
    try:
        _validate_managed_path(root, candidate)
        resolved = candidate.resolve(strict=True)
    except BackupError:
        raise
    except (OSError, RuntimeError) as error:
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            "declared source file is unavailable",
        ) from error
    if not _is_descendant(resolved, root) or not resolved.is_file() or _is_reparse_point(candidate):
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            "declared source file escaped its managed source root",
        )
    return resolved


def _validate_managed_path(root: Path, candidate: Path) -> None:
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            "managed path escaped its declared root",
        ) from error

    current = root
    try:
        for part in relative.parts:
            current /= part
            if _is_reparse_point(current):
                raise BackupError(
                    BackupFailureCode.INVALID_PATH,
                    "managed path contains a symbolic link or junction",
                )
            if not _is_descendant(current.resolve(strict=False), root):
                raise BackupError(
                    BackupFailureCode.INVALID_PATH,
                    "managed path component resolved outside its declared root",
                )
    except BackupError:
        raise
    except (OSError, RuntimeError) as error:
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            "managed path components could not be resolved safely",
        ) from error


def _require_ordinary_file(path: Path, field: str) -> None:
    try:
        if _is_reparse_point(path):
            raise BackupError(
                BackupFailureCode.INVALID_PATH,
                f"{field} must be an ordinary non-reparse file",
            )
        if not path.exists():
            raise BackupError(
                BackupFailureCode.INTEGRITY_FAILED,
                f"{field} is missing",
            )
        invalid = not path.is_file()
    except BackupError:
        raise
    except OSError as error:
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            f"{field} could not be inspected safely",
        ) from error
    if invalid:
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            f"{field} must be an ordinary non-reparse file",
        )


def _is_reparse_point(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _reject_overlap(left: Path, right: Path) -> None:
    if _is_descendant(left, right) or _is_descendant(right, left):
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            "source and destination roots must not overlap",
        )


def _is_descendant(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _require_new_targets(final: Path, staging: Path) -> None:
    if final.exists() or staging.exists():
        raise BackupError(
            BackupFailureCode.DESTINATION_EXISTS,
            "artifact destination or failed staging directory already exists",
        )


def _rename_new(source: Path, target: Path) -> None:
    if target.exists():
        raise BackupError(
            BackupFailureCode.DESTINATION_EXISTS,
            "immutable publish target already exists",
        )
    try:
        os.rename(source, target)
    except FileExistsError as error:
        raise BackupError(
            BackupFailureCode.DESTINATION_EXISTS,
            "immutable publish target appeared concurrently",
        ) from error
    except OSError as error:
        raise BackupError(
            BackupFailureCode.IO_FAILED,
            "atomic Windows rename failed",
        ) from error


def _preserve_failure(
    staging: Path,
    artifact_id: str,
    error: BackupError,
    failed_at: UtcTimestamp,
) -> None:
    if not staging.is_dir():
        return
    for marker in (_COMPLETE_FILE, _VERIFIED_FILE):
        source = staging / marker
        if source.exists():
            target = staging / f"{marker}.failed"
            if not target.exists():
                os.rename(source, target)
    record = FailureRecord(
        artifact_id=artifact_id,
        failed_at=failed_at,
        error_code=error.code,
        message=error.message,
    )
    _write_if_absent(staging / _FAILURE_FILE, _json_bytes(record.to_dict()))


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_new(path: Path, payload: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise BackupError(
            BackupFailureCode.DESTINATION_EXISTS,
            "immutable metadata file already exists",
        ) from error
    except OSError as error:
        raise BackupError(
            BackupFailureCode.IO_FAILED,
            "backup metadata write failed",
        ) from error


def _write_if_absent(path: Path, payload: bytes) -> None:
    if not path.exists():
        _write_new(path, payload)


def _read_bytes(path: Path, code: BackupFailureCode) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise BackupError(code, "required backup metadata is unavailable") from error


def _required_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            f"{field} must be non-empty text",
        )
    return value


def _required_non_negative_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BackupError(
            BackupFailureCode.MANIFEST_INVALID,
            f"{field} must be a non-negative integer",
        )
    return value


__all__ = [
    "BackupService",
    "load_snapshot_manifest",
    "object_path",
    "verify_local_snapshot",
    "verify_materialized_restore",
]
