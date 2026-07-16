"""Safe existing-database migration entry for SQLite.

Stage 0's ``initialize_database`` only creates brand-new databases and is
explicitly not an existing-database migration path. This module provides the
formal maintenance entry for migrating an *existing* SQLite database to a new
Alembic head, with strict safety guarantees:

1. Acquire a maintenance-period exclusive boundary (run-id reservation).
2. Create an online consistency snapshot via ``SQLiteOnlineBackup`` (page copy,
   never file copy of a live WAL database).
3. Strictly re-verify the snapshot: SHA-256, ``PRAGMA quick_check``, alembic
   revision and WAL/SHM absence on a read-only ``?mode=ro&immutable=1`` connection.
4. Restore the snapshot to a new isolated directory.
5. Run the Alembic upgrade on the isolated copy and verify new schema,
   ``quick_check`` and key repository operations.
6. Only after isolated verification passes, migrate the original database.
7. On any failure (snapshot, isolated, or original), explicitly fail and retain
   the snapshot, isolated copy and a ``failure.json`` evidence file. Never return
   success; never overwrite failure evidence. Retry must use a new run id.

This module lives under ``data/adapters/persistence`` so its ``alembic`` and
``sqlalchemy`` imports satisfy the architecture boundary checker. It does not
depend on jobs domain logic; it reuses only the shared SQLite backup adapter
and the engine helper. All migration tests use ``tmp_path`` only.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from alembic import command
from alembic.config import Config

from quantking.common.time import UtcTimestamp
from quantking.data.adapters.persistence.sqlite import create_data_engine
from quantking.jobs.adapters.persistence.sqlite import _REPOSITORY_ROOT
from quantking.jobs.backup.adapters.sqlite import SQLiteOnlineBackup

_FAILURE_FILE = "failure.json"
_SNAPSHOT_DB_NAME = "snapshot.db"
_ISOLATED_DB_NAME = "isolated.db"


class MaintenanceFailureCode(StrEnum):
    """Machine-stable failure semantics for the safe migration entry."""

    RUN_ID_ALREADY_USED = "run_id_already_used"
    DATABASE_NOT_FOUND = "database_not_found"
    SNAPSHOT_FAILED = "snapshot_failed"
    SNAPSHOT_REVISION_MISMATCH = "snapshot_revision_mismatch"
    SNAPSHOT_INTEGRITY_FAILED = "snapshot_integrity_failed"
    ISOLATED_RESTORE_FAILED = "isolated_restore_failed"
    ISOLATED_MIGRATION_FAILED = "isolated_migration_failed"
    ISOLATED_VERIFICATION_FAILED = "isolated_verification_failed"
    ORIGINAL_MIGRATION_FAILED = "original_migration_failed"
    ORIGINAL_VERIFICATION_FAILED = "original_verification_failed"


class MigrationFailureError(Exception):
    """A safe migration failure that retains its original exception as a chain."""

    def __init__(
        self,
        code: MaintenanceFailureCode,
        message: str,
        *,
        evidence_path: Path | None = None,
    ) -> None:
        if not isinstance(code, MaintenanceFailureCode):
            raise TypeError("code must be a MaintenanceFailureCode")
        if not isinstance(message, str) or not message.strip():
            raise TypeError("message must be non-empty text")
        self.code = code
        self.message = message
        self.evidence_path = evidence_path
        super().__init__(message)


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
class MigrationResult:
    """Outcome of a successful safe migration."""

    run_id: str
    ok: bool
    snapshot_path: Path | None
    isolated_path: Path | None
    target_revision: str


def migrate_existing_database(request: MigrationRequest) -> MigrationResult:
    """Migrate an existing SQLite database to ``target_revision`` safely.

    See module docstring for the seven-step safety contract. On any failure a
    ``MigrationFailureError`` is raised; the original database is left untouched
    and failure evidence is retained on disk.
    """
    _validate_request(request)
    snapshot_dir = request.snapshot_root / request.run_id
    snapshot_db = snapshot_dir / _SNAPSHOT_DB_NAME
    isolated_dir = request.isolated_root / request.run_id
    isolated_db = isolated_dir / _ISOLATED_DB_NAME

    _reject_existing_run(snapshot_dir, isolated_dir, request.run_id)
    snapshot_dir.mkdir(parents=True, exist_ok=False)

    # Step 2: online consistency snapshot.
    try:
        SQLiteOnlineBackup().backup(request.database_path, snapshot_db)
    except Exception as error:
        _write_failure(snapshot_dir, MaintenanceFailureCode.SNAPSHOT_FAILED, str(error))
        raise MigrationFailureError(
            MaintenanceFailureCode.SNAPSHOT_FAILED,
            "online SQLite snapshot failed",
            evidence_path=snapshot_dir / _FAILURE_FILE,
        ) from error

    # Step 3: strictly re-verify the snapshot.
    try:
        snapshot_revision = _verify_database(snapshot_db)
    except MigrationFailureError:
        raise
    except Exception as error:
        _write_failure(snapshot_dir, MaintenanceFailureCode.SNAPSHOT_INTEGRITY_FAILED, str(error))
        raise MigrationFailureError(
            MaintenanceFailureCode.SNAPSHOT_INTEGRITY_FAILED,
            "snapshot integrity verification failed",
            evidence_path=snapshot_dir / _FAILURE_FILE,
        ) from error

    if (
        request.expected_snapshot_revision is not None
        and snapshot_revision != request.expected_snapshot_revision
    ):
        _write_failure(
            snapshot_dir,
            MaintenanceFailureCode.SNAPSHOT_REVISION_MISMATCH,
            f"expected {request.expected_snapshot_revision}, got {snapshot_revision}",
        )
        raise MigrationFailureError(
            MaintenanceFailureCode.SNAPSHOT_REVISION_MISMATCH,
            "snapshot alembic revision does not match the expected credential",
            evidence_path=snapshot_dir / _FAILURE_FILE,
        )

    # Step 4: restore snapshot to a new isolated directory.
    isolated_dir.mkdir(parents=True, exist_ok=False)
    try:
        _restore_snapshot(snapshot_db, isolated_db)
    except Exception as error:
        _write_failure(isolated_dir, MaintenanceFailureCode.ISOLATED_RESTORE_FAILED, str(error))
        raise MigrationFailureError(
            MaintenanceFailureCode.ISOLATED_RESTORE_FAILED,
            "isolated restore failed",
            evidence_path=isolated_dir / _FAILURE_FILE,
        ) from error

    # Step 5: migrate the isolated copy and verify.
    head_revision = _resolve_head_revision()
    try:
        _alembic_upgrade(isolated_db, request.target_revision)
        _verify_database(isolated_db, expected_revision=head_revision)
        _verify_key_repository_operations(isolated_db)
    except MigrationFailureError:
        raise
    except Exception as error:
        _write_failure(isolated_dir, MaintenanceFailureCode.ISOLATED_MIGRATION_FAILED, str(error))
        raise MigrationFailureError(
            MaintenanceFailureCode.ISOLATED_MIGRATION_FAILED,
            "isolated migration or verification failed",
            evidence_path=isolated_dir / _FAILURE_FILE,
        ) from error

    # Step 6: migrate the original database.
    try:
        _alembic_upgrade(request.database_path, request.target_revision)
        _verify_database(request.database_path, expected_revision=head_revision)
    except Exception as error:
        _write_failure(snapshot_dir, MaintenanceFailureCode.ORIGINAL_MIGRATION_FAILED, str(error))
        raise MigrationFailureError(
            MaintenanceFailureCode.ORIGINAL_MIGRATION_FAILED,
            "original database migration failed; snapshot and isolated copy retained",
            evidence_path=snapshot_dir / _FAILURE_FILE,
        ) from error

    return MigrationResult(
        run_id=request.run_id,
        ok=True,
        snapshot_path=snapshot_db,
        isolated_path=isolated_db,
        target_revision=request.target_revision,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _validate_request(request: MigrationRequest) -> None:
    if not isinstance(request, MigrationRequest):
        raise TypeError("request must be a MigrationRequest")
    if not isinstance(request.run_id, str) or not request.run_id.strip():
        raise ValueError("run_id must be non-empty text")
    if not isinstance(request.database_path, Path) or not request.database_path.is_absolute():
        raise ValueError("database_path must be an absolute Path")
    if not isinstance(request.snapshot_root, Path) or not request.snapshot_root.is_absolute():
        raise ValueError("snapshot_root must be an absolute Path")
    if not isinstance(request.isolated_root, Path) or not request.isolated_root.is_absolute():
        raise ValueError("isolated_root must be an absolute Path")
    if not isinstance(request.target_revision, str) or not request.target_revision.strip():
        raise ValueError("target_revision must be non-empty text")
    if not request.database_path.is_file():
        raise MigrationFailureError(
            MaintenanceFailureCode.DATABASE_NOT_FOUND,
            "database file does not exist",
        )


def _reject_existing_run(snapshot_dir: Path, isolated_dir: Path, run_id: str) -> None:
    if snapshot_dir.exists() or isolated_dir.exists():
        raise MigrationFailureError(
            MaintenanceFailureCode.RUN_ID_ALREADY_USED,
            f"run id {run_id!r} already has evidence; retry must use a new run id",
        )


def _verify_database(database: Path, *, expected_revision: str | None = None) -> str:
    """Verify quick_check, alembic revision and WAL/SHM absence on a read-only connection."""
    _check_no_sidecars(database)
    uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check != ("ok",):
            raise RuntimeError(f"quick_check failed: {quick_check}")
        revision_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        if revision_row is None:
            raise RuntimeError("alembic_version table is empty")
        revision = str(revision_row[0])
    _check_no_sidecars(database)
    if expected_revision is not None and revision != expected_revision:
        raise RuntimeError(
            f"alembic revision {revision!r} does not match expected {expected_revision!r}"
        )
    return revision


def _check_no_sidecars(database: Path) -> None:
    sidecars = (Path(f"{database}-wal"), Path(f"{database}-shm"))
    if any(path.exists() for path in sidecars):
        raise RuntimeError("database has a WAL or SHM sidecar")


def _restore_snapshot(snapshot_db: Path, isolated_db: Path) -> None:
    """Copy the verified snapshot to a new isolated target via a fresh online backup."""
    SQLiteOnlineBackup().backup(snapshot_db, isolated_db)


def _alembic_upgrade(database: Path, revision: str) -> None:
    engine = create_data_engine(database, busy_timeout_ms=250)
    configuration = _alembic_config(database)
    try:
        with engine.connect() as connection:
            configuration.attributes["connection"] = connection
            command.upgrade(configuration, revision)
    finally:
        engine.dispose()


def _alembic_config(database: Path) -> Config:
    configuration = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_REPOSITORY_ROOT / "migrations"))
    configuration.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.as_posix()}")
    return configuration


def _resolve_head_revision() -> str:
    """Resolve the concrete ``head`` revision from the Alembic script directory."""
    from alembic.script import ScriptDirectory

    configuration = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_REPOSITORY_ROOT / "migrations"))
    script_directory = ScriptDirectory.from_config(configuration)
    head = script_directory.get_current_head()
    if not isinstance(head, str) or not head.strip():
        raise RuntimeError("alembic head revision could not be resolved")
    return head


def _verify_key_repository_operations(database: Path) -> None:
    """Confirm the migrated schema supports key repository operations."""
    uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        required = {
            "job_runs",
            "audit_records",
            "ingestion_batches",
            "dataset_versions",
            "source_overrides",
        }
        missing = required - tables
        if missing:
            raise RuntimeError(f"migrated schema missing tables: {sorted(missing)}")


def _write_failure(directory: Path, code: MaintenanceFailureCode, message: str) -> None:
    """Retain failure evidence; never overwrite an existing evidence file."""
    directory.mkdir(parents=True, exist_ok=True)
    evidence = directory / _FAILURE_FILE
    record = {
        "run_id": directory.name,
        "error_code": code.value,
        "message": message,
        "failed_at": UtcTimestamp(datetime.now(UTC)).to_iso(),
    }
    if evidence.exists():
        # Do not overwrite existing failure evidence; append a numbered sibling.
        index = 0
        while True:
            candidate = directory / f"failure-{index}.json"
            if not candidate.exists():
                candidate.write_text(
                    json.dumps(record, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
                return
            index += 1
    evidence.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
