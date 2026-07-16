"""Safe existing-database migration entry: snapshot, isolated verify, failure retention."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

from quantking.common.time import UtcTimestamp
from quantking.data.adapters.persistence.maintenance import (
    MaintenanceFailureCode,
    MigrationFailureError,
    MigrationRequest,
    migrate_existing_database,
)
from quantking.jobs.adapters.persistence.sqlite import (
    _REPOSITORY_ROOT,
    _reserve_new_database,
    create_sqlite_engine,
    initialize_database,
)

_NOW = UtcTimestamp(datetime(2026, 7, 16, 3, 0, tzinfo=UTC))


def _alembic_config(database: Path) -> Config:
    configuration = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_REPOSITORY_ROOT / "migrations"))
    configuration.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.as_posix()}")
    return configuration


def _upgrade(database: Path, revision: str = "head") -> None:
    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    configuration = _alembic_config(database)
    try:
        with engine.connect() as connection:
            configuration.attributes["connection"] = connection
            command.upgrade(configuration, revision)
    finally:
        engine.dispose()


def _build_stage0_database(tmp_path: Path) -> Path:
    """Build a real Stage 0 database at revision 20260715_0001 with sample data."""
    database = tmp_path / "run.db"
    _reserve_new_database(database)
    _upgrade(database, "20260715_0001")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO job_runs (job_id, job_type, idempotency_key, input_manifest_json, "
            "config_manifest_json, version_manifest_json, requester, status, created_at, "
            "started_at, ended_at, heartbeat_at, stage, progress, worker_id, log_id, "
            "artifacts_json, failure_json, cancel_requested_at, cancel_request, "
            "cancel_result, non_cancellable_phase, row_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "job:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "synthetic",
                "k1",
                "{}",
                "{}",
                "{}",
                "pytest",
                "SUCCEEDED",
                "2026-07-16T01:00:00Z",
                None,
                None,
                None,
                None,
                1.0,
                None,
                "logs/x.jsonl",
                "[]",
                None,
                None,
                None,
                None,
                None,
                1,
            ),
        )
        connection.execute(
            "INSERT INTO audit_records (actor, entity, action, correlation_id, "
            "occurred_at, details_json) VALUES (?,?,?,?,?,?)",
            ("pytest", "job:x", "job_created", "c", "2026-07-16T01:00:00Z", "{}"),
        )
        connection.commit()
    return database


class TestNewDatabaseMigration:
    def test_new_database_migrates_to_head(self, tmp_path: Path) -> None:
        database = tmp_path / "fresh.db"
        initialize_database(database)
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
            rev = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert rev == ("20260716_0001",)


class TestExistingDatabaseUpgrade:
    def test_stage0_to_stage1_upgrade_succeeds(self, tmp_path: Path) -> None:
        database = _build_stage0_database(tmp_path)
        snapshot_root = tmp_path / "snapshots"
        isolated_root = tmp_path / "isolated"
        request = MigrationRequest(
            run_id="mig-001",
            database_path=database,
            snapshot_root=snapshot_root,
            isolated_root=isolated_root,
            target_revision="head",
            requested_at=_NOW,
            code_version="abc123",
        )
        result = migrate_existing_database(request)
        assert result.ok
        assert result.snapshot_path is not None
        assert result.isolated_path is not None
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
            rev = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            # sample data preserved
            job = connection.execute(
                "SELECT status FROM job_runs WHERE job_id = ?",
                ("job:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",),
            ).fetchone()
        assert rev == ("20260716_0001",)
        assert job == ("SUCCEEDED",)

    def test_isolated_copy_is_verified_before_original_migration(self, tmp_path: Path) -> None:
        database = _build_stage0_database(tmp_path)
        snapshot_root = tmp_path / "snapshots"
        isolated_root = tmp_path / "isolated"
        request = MigrationRequest(
            run_id="mig-002",
            database_path=database,
            snapshot_root=snapshot_root,
            isolated_root=isolated_root,
            target_revision="head",
            requested_at=_NOW,
            code_version="abc123",
        )
        result = migrate_existing_database(request)
        assert result.ok
        # isolated copy should have been migrated to head and verified
        assert result.isolated_path is not None
        with sqlite3.connect(result.isolated_path) as connection:
            rev = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert rev == ("20260716_0001",)


class TestTamperedSnapshotRejected:
    def test_wrong_revision_snapshot_rejected(self, tmp_path: Path) -> None:
        database = _build_stage0_database(tmp_path)
        snapshot_root = tmp_path / "snapshots"
        isolated_root = tmp_path / "isolated"
        request = MigrationRequest(
            run_id="mig-bad-rev",
            database_path=database,
            snapshot_root=snapshot_root,
            isolated_root=isolated_root,
            target_revision="head",
            requested_at=_NOW,
            code_version="abc123",
            expected_snapshot_revision="9999_unknown",  # forged
        )
        with pytest.raises(MigrationFailureError) as exc_info:
            migrate_existing_database(request)
        assert exc_info.value.code is MaintenanceFailureCode.SNAPSHOT_REVISION_MISMATCH
        # original DB untouched
        with sqlite3.connect(database) as connection:
            rev = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert rev == ("20260715_0001",)


class TestIsolatedMigrationFailureRetainsEvidence:
    def test_isolated_migration_failure_keeps_evidence_and_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        database = _build_stage0_database(tmp_path)
        snapshot_root = tmp_path / "snapshots"
        isolated_root = tmp_path / "isolated"

        from quantking.data.adapters.persistence import maintenance as maint_mod

        def fail_isolated_upgrade(configuration: Any, revision: str) -> None:
            raise RuntimeError("synthetic isolated migration failure")

        monkeypatch.setattr(maint_mod.command, "upgrade", fail_isolated_upgrade)
        request = MigrationRequest(
            run_id="mig-iso-fail",
            database_path=database,
            snapshot_root=snapshot_root,
            isolated_root=isolated_root,
            target_revision="head",
            requested_at=_NOW,
            code_version="abc123",
        )
        with pytest.raises(MigrationFailureError) as exc_info:
            migrate_existing_database(request)
        assert exc_info.value.code is MaintenanceFailureCode.ISOLATED_MIGRATION_FAILED
        # failure evidence retained
        failure_files = list(snapshot_root.rglob("failure.json")) + list(
            isolated_root.rglob("failure.json")
        )
        assert failure_files, "failure evidence must be retained"
        # original DB untouched
        with sqlite3.connect(database) as connection:
            rev = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert rev == ("20260715_0001",)


class TestOriginalMigrationFailureRetainsEvidence:
    def test_original_migration_failure_retains_snapshot_isolated_and_evidence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        database = _build_stage0_database(tmp_path)
        snapshot_root = tmp_path / "snapshots"
        isolated_root = tmp_path / "isolated"

        call_count = {"n": 0}
        from quantking.data.adapters.persistence import maintenance as maint_mod

        original_upgrade = maint_mod.command.upgrade

        def fail_on_original(configuration: Any, revision: str) -> None:
            call_count["n"] += 1
            # first call(s) are the isolated copy; fail only when the original DB
            # is being migrated. We detect by the sqlalchemy.url in the config.
            url = configuration.get_main_option("sqlalchemy.url")
            if str(database.as_posix()) in (url or "") and "isolated" not in (url or ""):
                raise RuntimeError("synthetic original migration failure")
            original_upgrade(configuration, revision)

        monkeypatch.setattr(maint_mod.command, "upgrade", fail_on_original)
        request = MigrationRequest(
            run_id="mig-orig-fail",
            database_path=database,
            snapshot_root=snapshot_root,
            isolated_root=isolated_root,
            target_revision="head",
            requested_at=_NOW,
            code_version="abc123",
        )
        with pytest.raises(MigrationFailureError) as exc_info:
            migrate_existing_database(request)
        assert exc_info.value.code is MaintenanceFailureCode.ORIGINAL_MIGRATION_FAILED
        # snapshot and isolated copy retained
        assert snapshot_root.exists()
        # failure evidence present
        failure_files = list(snapshot_root.rglob("failure.json"))
        assert failure_files
        record = json.loads(failure_files[0].read_text(encoding="utf-8"))
        assert record["error_code"] == MaintenanceFailureCode.ORIGINAL_MIGRATION_FAILED.value


class TestDuplicateExecution:
    def test_duplicate_run_id_rejected(self, tmp_path: Path) -> None:
        database = _build_stage0_database(tmp_path)
        snapshot_root = tmp_path / "snapshots"
        isolated_root = tmp_path / "isolated"
        request = MigrationRequest(
            run_id="mig-dup",
            database_path=database,
            snapshot_root=snapshot_root,
            isolated_root=isolated_root,
            target_revision="head",
            requested_at=_NOW,
            code_version="abc123",
        )
        result = migrate_existing_database(request)
        assert result.ok
        # second invocation with the same run_id must fail
        with pytest.raises(MigrationFailureError) as exc_info:
            migrate_existing_database(request)
        assert exc_info.value.code is MaintenanceFailureCode.RUN_ID_ALREADY_USED
