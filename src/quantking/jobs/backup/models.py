"""Immutable S0-05 backup and restore contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from quantking.common.time import UtcTimestamp


class BackupCategory(StrEnum):
    """Stable payload categories recorded in every manifest."""

    DATABASE = "database"
    CONFIG = "config"
    MANIFEST = "manifest"
    PARTITION = "partition"
    ARTIFACT = "artifact"


class ArtifactState(StrEnum):
    """Directory lifecycle states; terminal states are never mutated in place."""

    INCOMPLETE = "incomplete"
    FAILED = "failed"
    COMPLETE = "complete"
    VERIFIED = "verified"


class BackupFailureCode(StrEnum):
    """Machine-stable S0-05 failure semantics."""

    INVALID_PATH = "invalid_path"
    DESTINATION_EXISTS = "destination_exists"
    INSUFFICIENT_SPACE = "insufficient_space"
    SOURCE_CHANGED = "source_changed"
    SQLITE_BACKUP_FAILED = "sqlite_backup_failed"
    SQLITE_BACKUP_TIMEOUT = "sqlite_backup_timeout"
    PAYLOAD_MISMATCH = "payload_mismatch"
    MANIFEST_INVALID = "manifest_invalid"
    OBJECT_CONFLICT = "object_conflict"
    INTEGRITY_FAILED = "integrity_failed"
    SCHEMA_MISMATCH = "schema_mismatch"
    IO_FAILED = "io_failed"


class BackupError(Exception):
    """A safe public failure that retains its original exception as a chain."""

    def __init__(self, code: BackupFailureCode, message: str) -> None:
        if not isinstance(code, BackupFailureCode):
            raise TypeError("code must be a BackupFailureCode")
        if not isinstance(message, str) or not message.strip():
            raise TypeError("message must be non-empty text")
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """One non-database source relative to a declared managed source root."""

    relative_path: str
    category: BackupCategory

    def with_path(self, relative_path: str) -> SourceSpec:
        return replace(self, relative_path=relative_path)


@dataclass(frozen=True, slots=True)
class LocalSnapshotRequest:
    """Explicit command for one new local recovery point."""

    run_id: str
    source_root: Path
    local_backup_root: Path
    database_relative_path: str
    files: tuple[SourceSpec, ...]
    created_at: UtcTimestamp
    code_version: str
    db_user_version: int
    alembic_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", tuple(self.files))

    def with_paths(
        self,
        *,
        source_root: Path | None = None,
        local_backup_root: Path | None = None,
    ) -> LocalSnapshotRequest:
        return replace(
            self,
            source_root=source_root if source_root is not None else self.source_root,
            local_backup_root=(
                local_backup_root if local_backup_root is not None else self.local_backup_root
            ),
        )

    def with_database_relative_path(self, relative_path: str) -> LocalSnapshotRequest:
        return replace(self, database_relative_path=relative_path)

    def with_run_id(self, run_id: str) -> LocalSnapshotRequest:
        return replace(self, run_id=run_id)

    def with_files(self, files: tuple[SourceSpec, ...]) -> LocalSnapshotRequest:
        return replace(self, files=files)


@dataclass(frozen=True, slots=True)
class InputItem:
    """Source-to-payload mapping without an absolute path."""

    source_relative_path: str
    payload_relative_path: str
    category: BackupCategory

    def to_dict(self) -> dict[str, object]:
        return {
            "source_relative_path": self.source_relative_path,
            "payload_relative_path": self.payload_relative_path,
            "category": self.category.value,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> InputItem:
        _require_exact_keys(
            raw,
            {"source_relative_path", "payload_relative_path", "category"},
            "input item",
        )
        return cls(
            source_relative_path=_required_text(raw, "source_relative_path"),
            payload_relative_path=_required_text(raw, "payload_relative_path"),
            category=BackupCategory(_required_text(raw, "category")),
        )


@dataclass(frozen=True, slots=True)
class InputManifest:
    """Stable input declaration written before any payload work begins."""

    schema_version: str
    run_id: str
    inputs: tuple[InputItem, ...]

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.inputs, key=lambda item: item.payload_relative_path))
        object.__setattr__(self, "inputs", ordered)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "inputs": [item.to_dict() for item in self.inputs],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> InputManifest:
        _require_exact_keys(raw, {"schema_version", "run_id", "inputs"}, "input manifest")
        return cls(
            schema_version=_required_text(raw, "schema_version"),
            run_id=_required_text(raw, "run_id"),
            inputs=tuple(
                InputItem.from_dict(_required_mapping(item))
                for item in _required_list(raw, "inputs")
            ),
        )


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One exact payload object in a recovery point."""

    relative_path: str
    size: int
    sha256: str
    category: BackupCategory

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "size": self.size,
            "sha256": self.sha256,
            "category": self.category.value,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> FileEntry:
        _require_exact_keys(
            raw,
            {"relative_path", "size", "sha256", "category"},
            "file entry",
        )
        return cls(
            relative_path=_required_text(raw, "relative_path"),
            size=_required_int(raw, "size"),
            sha256=_required_text(raw, "sha256"),
            category=BackupCategory(_required_text(raw, "category")),
        )


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """Self-contained, immutable local/offsite restore-point manifest."""

    schema_version: str
    restore_point_id: str
    created_at: UtcTimestamp
    code_version: str
    db_user_version: int
    alembic_version: str
    input_manifest_sha256: str
    inputs: tuple[InputItem, ...]
    files: tuple[FileEntry, ...]

    def __post_init__(self) -> None:
        inputs = tuple(sorted(self.inputs, key=lambda item: item.payload_relative_path))
        files = tuple(sorted(self.files, key=lambda item: item.relative_path))
        if len({item.payload_relative_path for item in inputs}) != len(inputs):
            raise ValueError("input payload paths must be unique")
        if len({item.relative_path for item in files}) != len(files):
            raise ValueError("manifest file paths must be unique")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "files", files)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "restore_point_id": self.restore_point_id,
            "created_at": self.created_at.to_iso(),
            "code_version": self.code_version,
            "db_user_version": self.db_user_version,
            "alembic_version": self.alembic_version,
            "input_manifest_sha256": self.input_manifest_sha256,
            "inputs": [item.to_dict() for item in self.inputs],
            "files": [item.to_dict() for item in self.files],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> SnapshotManifest:
        _require_exact_keys(
            raw,
            {
                "schema_version",
                "restore_point_id",
                "created_at",
                "code_version",
                "db_user_version",
                "alembic_version",
                "input_manifest_sha256",
                "inputs",
                "files",
            },
            "snapshot manifest",
        )
        return cls(
            schema_version=_required_text(raw, "schema_version"),
            restore_point_id=_required_text(raw, "restore_point_id"),
            created_at=UtcTimestamp.from_iso(_required_text(raw, "created_at")),
            code_version=_required_text(raw, "code_version"),
            db_user_version=_required_int(raw, "db_user_version"),
            alembic_version=_required_text(raw, "alembic_version"),
            input_manifest_sha256=_required_text(raw, "input_manifest_sha256"),
            inputs=tuple(
                InputItem.from_dict(_required_mapping(item))
                for item in _required_list(raw, "inputs")
            ),
            files=tuple(
                FileEntry.from_dict(_required_mapping(item))
                for item in _required_list(raw, "files")
            ),
        )


@dataclass(frozen=True, slots=True)
class LocalSnapshotResult:
    path: Path
    state: ArtifactState
    manifest: SnapshotManifest


@dataclass(frozen=True, slots=True)
class VerificationResult:
    state: ArtifactState
    manifest: SnapshotManifest


@dataclass(frozen=True, slots=True)
class ReplicationReport:
    schema_version: str
    restore_point_id: str
    created_at: UtcTimestamp
    total_objects: int
    copied: int
    skipped: int
    conflicted: int
    state: ArtifactState

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "restore_point_id": self.restore_point_id,
            "created_at": self.created_at.to_iso(),
            "total_objects": self.total_objects,
            "copied": self.copied,
            "skipped": self.skipped,
            "conflicted": self.conflicted,
            "state": self.state.value,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ReplicationReport:
        _require_exact_keys(
            raw,
            {
                "schema_version",
                "restore_point_id",
                "created_at",
                "total_objects",
                "copied",
                "skipped",
                "conflicted",
                "state",
            },
            "replication report",
        )
        return cls(
            schema_version=_required_text(raw, "schema_version"),
            restore_point_id=_required_text(raw, "restore_point_id"),
            created_at=UtcTimestamp.from_iso(_required_text(raw, "created_at")),
            total_objects=_required_int(raw, "total_objects"),
            copied=_required_int(raw, "copied"),
            skipped=_required_int(raw, "skipped"),
            conflicted=_required_int(raw, "conflicted"),
            state=ArtifactState(_required_text(raw, "state")),
        )


@dataclass(frozen=True, slots=True)
class RestoreResult:
    path: Path
    state: ArtifactState
    manifest: SnapshotManifest


@dataclass(frozen=True, slots=True)
class FailureRecord:
    artifact_id: str
    failed_at: UtcTimestamp
    error_code: BackupFailureCode
    message: str
    state: ArtifactState = ArtifactState.FAILED

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "failed_at": self.failed_at.to_iso(),
            "error_code": self.error_code.value,
            "message": self.message,
            "state": self.state.value,
        }


def _required_text(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be non-empty text")
    return value


def _required_int(raw: Mapping[str, object], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _required_list(raw: Mapping[str, object], key: str) -> list[object]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _required_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("manifest item must be an object")
    return value


def _require_exact_keys(raw: Mapping[str, object], expected: set[str], document: str) -> None:
    if set(raw) != expected:
        raise ValueError(f"{document} fields do not match the supported schema")
