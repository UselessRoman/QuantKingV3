"""Safe existing-database migration with full Stage 0 snapshot evidence.

The original database is protected by both an exclusive maintenance lock file
and a SQLite ``BEGIN IMMEDIATE`` writer boundary before the snapshot starts.
That boundary remains held through snapshot creation, isolated restore,
isolated migration, all pre-original tamper checks, the original migration and
its final verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection

from quantking.common.paths import RuntimePaths
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.adapters.persistence.sqlite import create_data_engine
from quantking.jobs.adapters.persistence.sqlite import _REPOSITORY_ROOT
from quantking.jobs.backup.adapters.sqlite import SQLiteOnlineBackup
from quantking.jobs.backup.models import (
    BackupError,
    LocalSnapshotRequest,
    LocalSnapshotResult,
    RestoreResult,
)
from quantking.jobs.backup.services import (
    BackupService,
    verify_local_snapshot,
    verify_materialized_restore,
)

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_FAILURE_FILE = "failure.json"
_EVIDENCE_SCHEMA = "stage1-migration-evidence.v1"
_RESTORED_DATABASE = Path("payload/database/quantking.db")
_ISOLATED_DATABASE = "isolated-migrated.db"


class MaintenanceFailureCode(StrEnum):
    """Machine-stable and path-safe migration failure semantics."""

    INVALID_REQUEST = "invalid_request"
    INVALID_PATH = "invalid_path"
    RUN_ID_ALREADY_USED = "run_id_already_used"
    MAINTENANCE_LOCKED = "maintenance_locked"
    DATABASE_NOT_FOUND = "database_not_found"
    TARGET_REVISION_INVALID = "target_revision_invalid"
    SNAPSHOT_FAILED = "snapshot_failed"
    SNAPSHOT_REVISION_MISMATCH = "snapshot_revision_mismatch"
    SNAPSHOT_INTEGRITY_FAILED = "snapshot_integrity_failed"
    ISOLATED_RESTORE_FAILED = "isolated_restore_failed"
    ISOLATED_MIGRATION_FAILED = "isolated_migration_failed"
    ISOLATED_VERIFICATION_FAILED = "isolated_verification_failed"
    EVIDENCE_INTEGRITY_FAILED = "evidence_integrity_failed"
    PRE_ORIGINAL_REVALIDATION_FAILED = "pre_original_revalidation_failed"
    ORIGINAL_MIGRATION_FAILED = "original_migration_failed"
    ORIGINAL_VERIFICATION_FAILED = "original_verification_failed"


_FAILURE_MESSAGES: dict[MaintenanceFailureCode, str] = {
    MaintenanceFailureCode.INVALID_REQUEST: "migration request is invalid",
    MaintenanceFailureCode.INVALID_PATH: "migration paths failed managed-path validation",
    MaintenanceFailureCode.RUN_ID_ALREADY_USED: "migration run id already has immutable evidence",
    MaintenanceFailureCode.MAINTENANCE_LOCKED: "another migration holds the maintenance boundary",
    MaintenanceFailureCode.DATABASE_NOT_FOUND: "migration database does not exist",
    MaintenanceFailureCode.TARGET_REVISION_INVALID: "target Alembic revision is invalid",
    MaintenanceFailureCode.SNAPSHOT_FAILED: "Stage 0 snapshot creation failed",
    MaintenanceFailureCode.SNAPSHOT_REVISION_MISMATCH: "source revision credential did not match",
    MaintenanceFailureCode.SNAPSHOT_INTEGRITY_FAILED: "Stage 0 snapshot verification failed",
    MaintenanceFailureCode.ISOLATED_RESTORE_FAILED: "Stage 0 isolated restore failed",
    MaintenanceFailureCode.ISOLATED_MIGRATION_FAILED: "isolated migration failed",
    MaintenanceFailureCode.ISOLATED_VERIFICATION_FAILED: "isolated migration verification failed",
    MaintenanceFailureCode.EVIDENCE_INTEGRITY_FAILED: "migration evidence integrity failed",
    MaintenanceFailureCode.PRE_ORIGINAL_REVALIDATION_FAILED: (
        "pre-original snapshot or restore revalidation failed"
    ),
    MaintenanceFailureCode.ORIGINAL_MIGRATION_FAILED: "original database migration failed",
    MaintenanceFailureCode.ORIGINAL_VERIFICATION_FAILED: (
        "original database migration verification failed"
    ),
}


class MigrationFailureError(Exception):
    """A stable public failure that keeps sensitive details only in its exception chain."""

    def __init__(
        self,
        code: MaintenanceFailureCode,
        *,
        evidence_path: Path | None = None,
    ) -> None:
        if not isinstance(code, MaintenanceFailureCode):
            raise TypeError("code must be a MaintenanceFailureCode")
        self.code = code
        self.message = _FAILURE_MESSAGES[code]
        self.evidence_path = evidence_path
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class MigrationRequest:
    """Explicit command to migrate one existing SQLite database safely."""

    run_id: str
    database_path: Path
    snapshot_root: Path
    isolated_root: Path
    target_revision: str
    requested_at: UtcTimestamp
    code_version: str
    expected_snapshot_revision: str | None = field(default=None)


@dataclass(frozen=True, slots=True)
class MigrationCheckpoint:
    """Read-only paths exposed only to attack/concurrency regression hooks."""

    snapshot_path: Path
    isolated_restore_path: Path
    isolated_database_path: Path
    evidence_path: Path


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Outcome and concrete immutable credentials of a successful migration."""

    run_id: str
    ok: bool
    snapshot_path: Path
    isolated_restore_path: Path
    isolated_path: Path
    evidence_path: Path
    source_revision: str
    target_revision: str
    snapshot_id: str
    manifest_sha256: str


def migrate_existing_database(
    request: MigrationRequest,
    *,
    before_original_migration: Callable[[MigrationCheckpoint], None] | None = None,
) -> MigrationResult:
    """Migrate an existing database only after full snapshot and isolated proof."""
    _validate_request(request)
    target_revision = _resolve_target_revision(request.target_revision)
    snapshot_path = request.snapshot_root / request.run_id
    isolated_restore_path = request.isolated_root / request.run_id
    isolated_work_dir = request.isolated_root / f"{request.run_id}-migration"
    isolated_database = isolated_work_dir / _ISOLATED_DATABASE
    evidence_path = request.snapshot_root / "migration-evidence" / f"{request.run_id}.json"
    evidence_sha_path = evidence_path.with_suffix(".sha256")
    failure_dir = request.snapshot_root / "migration-failures" / request.run_id
    _reject_existing_run(
        snapshot_path,
        isolated_restore_path,
        isolated_work_dir,
        evidence_path,
        evidence_sha_path,
        failure_dir,
    )

    with _maintenance_lock(request.database_path, request.run_id):
        engine = create_data_engine(request.database_path, busy_timeout_ms=250)
        try:
            with engine.connect() as connection:
                try:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                except Exception as error:
                    raise MigrationFailureError(
                        MaintenanceFailureCode.MAINTENANCE_LOCKED
                    ) from error
                try:
                    try:
                        source_revision, user_version = _verify_connection(connection)
                    except Exception as error:
                        raise MigrationFailureError(
                            MaintenanceFailureCode.SNAPSHOT_INTEGRITY_FAILED
                        ) from error
                    if (
                        request.expected_snapshot_revision is not None
                        and source_revision != request.expected_snapshot_revision
                    ):
                        raise MigrationFailureError(
                            MaintenanceFailureCode.SNAPSHOT_REVISION_MISMATCH
                        )

                    snapshot = _create_snapshot(
                        request,
                        source_revision=source_revision,
                        user_version=user_version,
                    )
                    snapshot_path = snapshot.path
                    try:
                        verified_snapshot = verify_local_snapshot(snapshot_path)
                    except Exception as error:
                        raise MigrationFailureError(
                            MaintenanceFailureCode.SNAPSHOT_INTEGRITY_FAILED
                        ) from error
                    try:
                        manifest_bytes = (snapshot_path / "manifest.json").read_bytes()
                        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
                        complete_sha256 = hashlib.sha256(
                            (snapshot_path / "COMPLETE").read_bytes()
                        ).hexdigest()
                    except OSError as error:
                        raise MigrationFailureError(
                            MaintenanceFailureCode.SNAPSHOT_INTEGRITY_FAILED
                        ) from error
                    if verified_snapshot.manifest.alembic_version != source_revision:
                        raise MigrationFailureError(
                            MaintenanceFailureCode.SNAPSHOT_REVISION_MISMATCH
                        )

                    restored = _restore_snapshot(
                        request,
                        snapshot_path=snapshot_path,
                    )
                    isolated_restore_path = restored.path
                    restored_database = isolated_restore_path / _RESTORED_DATABASE
                    try:
                        verify_materialized_restore(isolated_restore_path)
                    except Exception as error:
                        raise MigrationFailureError(
                            MaintenanceFailureCode.ISOLATED_RESTORE_FAILED
                        ) from error

                    isolated_work_dir.mkdir(parents=False, exist_ok=False)
                    try:
                        SQLiteOnlineBackup().backup(restored_database, isolated_database)
                        _alembic_upgrade(isolated_database, target_revision)
                    except Exception as error:
                        raise MigrationFailureError(
                            MaintenanceFailureCode.ISOLATED_MIGRATION_FAILED
                        ) from error
                    try:
                        _verify_database(isolated_database, expected_revision=target_revision)
                        _verify_key_repository_operations(isolated_database)
                    except Exception as error:
                        raise MigrationFailureError(
                            MaintenanceFailureCode.ISOLATED_VERIFICATION_FAILED
                        ) from error

                    evidence = {
                        "schema_version": _EVIDENCE_SCHEMA,
                        "run_id": request.run_id,
                        "requested_at": request.requested_at.to_iso(),
                        "code_version": request.code_version,
                        "source_revision": source_revision,
                        "target_revision": target_revision,
                        "snapshot_id": verified_snapshot.manifest.restore_point_id,
                        "manifest_sha256": manifest_sha256,
                        "complete_sha256": complete_sha256,
                    }
                    _write_immutable_evidence(evidence_path, evidence_sha_path, evidence)

                    checkpoint = MigrationCheckpoint(
                        snapshot_path=snapshot_path,
                        isolated_restore_path=isolated_restore_path,
                        isolated_database_path=isolated_database,
                        evidence_path=evidence_path,
                    )
                    if before_original_migration is not None:
                        try:
                            before_original_migration(checkpoint)
                        except Exception as error:
                            raise MigrationFailureError(
                                MaintenanceFailureCode.PRE_ORIGINAL_REVALIDATION_FAILED
                            ) from error

                    try:
                        reverified = verify_local_snapshot(snapshot_path)
                        verify_materialized_restore(isolated_restore_path)
                        _verify_database(isolated_database, expected_revision=target_revision)
                        _verify_evidence(evidence_path, evidence_sha_path, evidence)
                        current_revision, _ = _verify_connection(connection)
                        if current_revision != source_revision:
                            raise RuntimeError(
                                "source revision changed inside maintenance boundary"
                            )
                        current_manifest_sha = hashlib.sha256(
                            (snapshot_path / "manifest.json").read_bytes()
                        ).hexdigest()
                        current_complete_sha = hashlib.sha256(
                            (snapshot_path / "COMPLETE").read_bytes()
                        ).hexdigest()
                        if (
                            reverified.manifest.restore_point_id != request.run_id
                            or current_manifest_sha != manifest_sha256
                            or current_complete_sha != complete_sha256
                        ):
                            raise RuntimeError("snapshot identity changed")
                    except Exception as error:
                        raise MigrationFailureError(
                            MaintenanceFailureCode.PRE_ORIGINAL_REVALIDATION_FAILED
                        ) from error

                    try:
                        _alembic_upgrade_connection(connection, target_revision)
                    except Exception as error:
                        raise MigrationFailureError(
                            MaintenanceFailureCode.ORIGINAL_MIGRATION_FAILED
                        ) from error
                    try:
                        final_revision, _ = _verify_connection(
                            connection, expected_revision=target_revision
                        )
                    except Exception as error:
                        raise MigrationFailureError(
                            MaintenanceFailureCode.ORIGINAL_VERIFICATION_FAILED
                        ) from error
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except MigrationFailureError as error:
            failure_evidence = _write_failure(failure_dir, request.run_id, error.code)
            raise MigrationFailureError(error.code, evidence_path=failure_evidence) from error
        finally:
            engine.dispose()

    return MigrationResult(
        run_id=request.run_id,
        ok=True,
        snapshot_path=snapshot_path,
        isolated_restore_path=isolated_restore_path,
        isolated_path=isolated_database,
        evidence_path=evidence_path,
        source_revision=source_revision,
        target_revision=final_revision,
        snapshot_id=request.run_id,
        manifest_sha256=manifest_sha256,
    )


def _validate_request(request: MigrationRequest) -> None:
    if not isinstance(request, MigrationRequest):
        raise TypeError("request must be a MigrationRequest")
    if (
        not isinstance(request.run_id, str)
        or _RUN_ID.fullmatch(request.run_id) is None
        or ".." in request.run_id
        or request.run_id != request.run_id.strip()
        or request.run_id.endswith(".incomplete")
    ):
        raise MigrationFailureError(MaintenanceFailureCode.INVALID_REQUEST)
    if not isinstance(request.requested_at, UtcTimestamp):
        raise MigrationFailureError(MaintenanceFailureCode.INVALID_REQUEST)
    for value in (request.target_revision, request.code_version):
        if not isinstance(value, str) or not value or value != value.strip():
            raise MigrationFailureError(MaintenanceFailureCode.INVALID_REQUEST)
    for path in (request.database_path, request.snapshot_root, request.isolated_root):
        if not isinstance(path, Path) or not path.is_absolute():
            raise MigrationFailureError(MaintenanceFailureCode.INVALID_PATH)
    if not request.database_path.is_file():
        raise MigrationFailureError(MaintenanceFailureCode.DATABASE_NOT_FOUND)
    try:
        RuntimePaths(
            code_root=request.database_path.parent,
            data_root=request.snapshot_root,
            offsite_backup_root=request.isolated_root,
        )
    except Exception as error:
        raise MigrationFailureError(MaintenanceFailureCode.INVALID_PATH) from error


def _resolve_target_revision(requested: str) -> str:
    from alembic.script import ScriptDirectory

    configuration = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_REPOSITORY_ROOT / "migrations"))
    try:
        revision = ScriptDirectory.from_config(configuration).get_revision(requested)
    except Exception as error:
        raise MigrationFailureError(MaintenanceFailureCode.TARGET_REVISION_INVALID) from error
    if revision is None or not revision.revision:
        raise MigrationFailureError(MaintenanceFailureCode.TARGET_REVISION_INVALID)
    return revision.revision


def _reject_existing_run(*paths: Path) -> None:
    if any(path.exists() for path in paths):
        raise MigrationFailureError(MaintenanceFailureCode.RUN_ID_ALREADY_USED)


@contextmanager
def _maintenance_lock(database: Path, run_id: str) -> Iterator[None]:
    lock_path = database.with_name(f".{database.name}.migration.lock")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError as error:
        raise MigrationFailureError(MaintenanceFailureCode.MAINTENANCE_LOCKED) from error
    try:
        payload = json.dumps(
            {"schema_version": "1", "run_id": run_id},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        os.write(descriptor, payload)
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)
        with suppress(FileNotFoundError):
            lock_path.unlink()


def _create_snapshot(
    request: MigrationRequest,
    *,
    source_revision: str,
    user_version: int,
) -> LocalSnapshotResult:
    try:
        return BackupService().create_local_snapshot(
            LocalSnapshotRequest(
                run_id=request.run_id,
                source_root=request.database_path.parent,
                local_backup_root=request.snapshot_root,
                database_relative_path=request.database_path.name,
                files=(),
                created_at=request.requested_at,
                code_version=request.code_version,
                db_user_version=user_version,
                alembic_version=source_revision,
            )
        )
    except BackupError as error:
        raise MigrationFailureError(MaintenanceFailureCode.SNAPSHOT_FAILED) from error


def _restore_snapshot(request: MigrationRequest, *, snapshot_path: Path) -> RestoreResult:
    try:
        return BackupService().restore_from_local(
            snapshot_path,
            request.isolated_root,
            request.run_id,
            verified_at=request.requested_at,
        )
    except BackupError as error:
        raise MigrationFailureError(MaintenanceFailureCode.ISOLATED_RESTORE_FAILED) from error


def _alembic_config(database: Path | None = None) -> Config:
    configuration = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_REPOSITORY_ROOT / "migrations"))
    if database is not None:
        configuration.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.as_posix()}")
    return configuration


def _alembic_upgrade(database: Path, revision: str) -> None:
    engine = create_data_engine(database, busy_timeout_ms=250)
    try:
        with engine.connect() as connection:
            _alembic_upgrade_connection(connection, revision)
            connection.commit()
    finally:
        engine.dispose()


def _alembic_upgrade_connection(connection: Connection, revision: str) -> None:
    configuration = _alembic_config()
    configuration.attributes["connection"] = connection
    command.upgrade(configuration, revision)


def _verify_connection(
    connection: Connection, *, expected_revision: str | None = None
) -> tuple[str, int]:
    quick_check = connection.exec_driver_sql("PRAGMA quick_check").fetchone()
    if quick_check != ("ok",):
        raise RuntimeError("quick_check failed")
    revision_row = connection.exec_driver_sql("SELECT version_num FROM alembic_version").fetchone()
    if revision_row is None:
        raise RuntimeError("alembic revision is missing")
    revision = str(revision_row[0])
    if expected_revision is not None and revision != expected_revision:
        raise RuntimeError("alembic revision does not match expected revision")
    user_version_row = connection.exec_driver_sql("PRAGMA user_version").fetchone()
    user_version = 0 if user_version_row is None else int(user_version_row[0])
    return revision, user_version


def _verify_database(database: Path, *, expected_revision: str) -> None:
    for suffix in ("-wal", "-shm"):
        if Path(f"{database}{suffix}").exists():
            raise RuntimeError("isolated database has SQLite sidecars")
    import sqlite3

    uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("quick_check failed")
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    if revision != (expected_revision,):
        raise RuntimeError("isolated revision mismatch")


def _verify_key_repository_operations(database: Path) -> None:
    import sqlite3

    uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    required = {
        "job_runs",
        "audit_records",
        "ingestion_batches",
        "dataset_versions",
        "dataset_version_batches",
        "quality_runs",
        "quality_results",
        "difference_reports",
        "source_overrides",
    }
    if not required.issubset(tables):
        raise RuntimeError("migrated schema is incomplete")


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_immutable_evidence(
    evidence_path: Path,
    sha_path: Path,
    evidence: Mapping[str, object],
) -> None:
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_bytes(evidence)
    digest = hashlib.sha256(payload).hexdigest().encode("ascii") + b"\n"
    try:
        with evidence_path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        with sha_path.open("xb") as stream:
            stream.write(digest)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise MigrationFailureError(MaintenanceFailureCode.EVIDENCE_INTEGRITY_FAILED) from error


def _verify_evidence(
    evidence_path: Path,
    sha_path: Path,
    expected: Mapping[str, object],
) -> None:
    payload = evidence_path.read_bytes()
    digest = sha_path.read_text(encoding="ascii").strip()
    if payload != _canonical_bytes(expected) or hashlib.sha256(payload).hexdigest() != digest:
        raise RuntimeError("migration evidence changed")


def _write_failure(directory: Path, run_id: str, code: MaintenanceFailureCode) -> Path:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None or ".." in run_id:
        raise MigrationFailureError(MaintenanceFailureCode.INVALID_REQUEST)
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": "1",
        "run_id": run_id,
        "error_code": code.value,
        "message": _FAILURE_MESSAGES[code],
        "failed_at": utc_now().to_iso(),
    }
    payload = _canonical_bytes(record)
    index = -1
    while True:
        name = _FAILURE_FILE if index < 0 else f"failure-{index}.json"
        path = directory / name
        try:
            with path.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            index += 1
            continue
        return path
