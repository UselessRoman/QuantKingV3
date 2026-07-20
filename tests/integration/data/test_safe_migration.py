"""Safe migration attacks, maintenance exclusivity, and Stage 0 snapshot proof."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

from quantking.common.paths import is_reparse_point
from quantking.common.time import UtcTimestamp
from quantking.data.adapters.persistence.maintenance import (
    MaintenanceFailureCode,
    MigrationCheckpoint,
    MigrationFailureError,
    MigrationRequest,
    _write_failure,
    migrate_existing_database,
)
from quantking.jobs.adapters.persistence.sqlite import (
    _REPOSITORY_ROOT,
    _reserve_new_database,
    create_sqlite_engine,
)
from quantking.jobs.backup.services import (
    verify_local_snapshot,
    verify_materialized_restore,
)

_NOW = UtcTimestamp(datetime(2026, 7, 16, 3, 0, tzinfo=UTC))
_STAGE0_REVISION = "20260715_0001"
_HEAD = "20260716_0005"


def _alembic_config(database: Path) -> Config:
    configuration = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_REPOSITORY_ROOT / "migrations"))
    configuration.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.as_posix()}")
    return configuration


def _upgrade(database: Path, revision: str) -> None:
    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    try:
        with engine.connect() as connection:
            configuration = _alembic_config(database)
            configuration.attributes["connection"] = connection
            command.upgrade(configuration, revision)
    finally:
        engine.dispose()


def _build_stage0_database(tmp_path: Path) -> Path:
    database_root = tmp_path / "database"
    database_root.mkdir()
    database = database_root / "run.db"
    _reserve_new_database(database)
    _upgrade(database, _STAGE0_REVISION)
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
        connection.commit()
    return database


def _request(
    tmp_path: Path,
    database: Path,
    *,
    run_id: str = "mig-001",
    snapshot_name: str = "snapshots",
    isolated_name: str = "isolated",
) -> MigrationRequest:
    return MigrationRequest(
        run_id=run_id,
        database_path=database,
        snapshot_root=tmp_path / snapshot_name,
        isolated_root=tmp_path / isolated_name,
        target_revision="head",
        requested_at=_NOW,
        code_version="98a8440dc2b860f4dd46ae592c94ea832f89e811",
        expected_snapshot_revision=_STAGE0_REVISION,
    )


def _revision(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def _child_attempt(request: MigrationRequest, queue: Any) -> None:
    try:
        migrate_existing_database(request)
    except MigrationFailureError as error:
        queue.put(error.code.value)
    else:
        queue.put("unexpected_success")


def _artifact_inventory(*paths: Path) -> dict[str, bytes]:
    inventory: dict[str, bytes] = {}
    for index, path in enumerate(paths):
        if path.is_file():
            inventory[f"{index}:{path.name}"] = path.read_bytes()
            continue
        inventory[f"{index}:directory"] = b""
        for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
            inventory[f"{index}:{file_path.relative_to(path).as_posix()}"] = file_path.read_bytes()
    return inventory


class TestCompleteSnapshotAndEvidence:
    def test_stage0_snapshot_restore_and_concrete_revision_evidence(self, tmp_path: Path) -> None:
        database = _build_stage0_database(tmp_path)
        request = _request(tmp_path, database)
        result = migrate_existing_database(request)

        assert result.ok
        assert result.source_revision == _STAGE0_REVISION
        assert result.target_revision == _HEAD
        assert result.target_revision != "head"
        assert _revision(database) == _HEAD
        assert (
            verify_local_snapshot(result.snapshot_path).manifest.restore_point_id == request.run_id
        )
        verify_materialized_restore(result.isolated_restore_path)
        assert (result.snapshot_path / "input.json").is_file()
        assert (result.snapshot_path / "manifest.json").is_file()
        assert (result.snapshot_path / "COMPLETE").is_file()
        assert (result.snapshot_path / "payload" / "database" / "quantking.db").is_file()
        assert (result.isolated_restore_path / "VERIFIED").is_file()
        assert _revision(result.isolated_path) == _HEAD

        evidence = json.loads(result.evidence_path.read_text(encoding="utf-8"))
        assert evidence == {
            "schema_version": "stage1-migration-evidence.v1",
            "run_id": request.run_id,
            "requested_at": _NOW.to_iso(),
            "code_version": request.code_version,
            "source_revision": _STAGE0_REVISION,
            "target_revision": _HEAD,
            "snapshot_id": request.run_id,
            "manifest_sha256": result.manifest_sha256,
            "complete_sha256": hashlib.sha256(
                (result.snapshot_path / "COMPLETE").read_bytes()
            ).hexdigest(),
        }
        evidence_bytes = result.evidence_path.read_bytes()
        evidence_sha = result.evidence_path.with_suffix(".sha256").read_text().strip()
        assert hashlib.sha256(evidence_bytes).hexdigest() == evidence_sha


class TestTamperingBeforeOriginalMigration:
    @pytest.mark.parametrize("attack", ("manifest", "marker", "database", "revision"))
    def test_tampering_is_rejected_before_original(self, tmp_path: Path, attack: str) -> None:
        database = _build_stage0_database(tmp_path)
        request = _request(tmp_path, database, run_id=f"mig-tamper-{attack}")

        def tamper(checkpoint: MigrationCheckpoint) -> None:
            if attack == "manifest":
                path = checkpoint.snapshot_path / "manifest.json"
                path.write_bytes(path.read_bytes() + b" ")
            elif attack == "marker":
                path = checkpoint.snapshot_path / "COMPLETE"
                path.write_bytes(path.read_bytes() + b" ")
            elif attack == "database":
                path = checkpoint.snapshot_path / "payload" / "database" / "quantking.db"
                path.write_bytes(path.read_bytes() + b"attack")
            else:
                with sqlite3.connect(checkpoint.isolated_database_path) as connection:
                    connection.execute("UPDATE alembic_version SET version_num='forged_revision'")
                    connection.commit()

        with pytest.raises(MigrationFailureError) as exc_info:
            migrate_existing_database(request, before_original_migration=tamper)
        assert exc_info.value.code is MaintenanceFailureCode.PRE_ORIGINAL_REVALIDATION_FAILED
        assert _revision(database) == _STAGE0_REVISION
        assert exc_info.value.evidence_path is not None
        record = json.loads(exc_info.value.evidence_path.read_text(encoding="utf-8"))
        rendered = json.dumps(record, ensure_ascii=False)
        assert str(database) not in rendered
        assert "attack" not in rendered

    def test_expected_source_revision_mismatch_stops_before_original(self, tmp_path: Path) -> None:
        database = _build_stage0_database(tmp_path)
        request = _request(tmp_path, database, run_id="mig-source-revision")
        request = MigrationRequest(
            run_id=request.run_id,
            database_path=request.database_path,
            snapshot_root=request.snapshot_root,
            isolated_root=request.isolated_root,
            target_revision=request.target_revision,
            requested_at=request.requested_at,
            code_version=request.code_version,
            expected_snapshot_revision="forged_revision",
        )
        with pytest.raises(MigrationFailureError) as exc_info:
            migrate_existing_database(request)
        assert exc_info.value.code is MaintenanceFailureCode.SNAPSHOT_REVISION_MISMATCH
        assert _revision(database) == _STAGE0_REVISION


class TestRunIdAndManagedPathAttacks:
    @pytest.mark.parametrize(
        "run_id",
        (
            "../escape",
            "C:/absolute",
            "a/b",
            "a\\b",
            " white",
            "white ",
            "two words",
            "dot..escape",
            "failed.incomplete",
        ),
    )
    def test_run_id_attack_is_rejected(self, tmp_path: Path, run_id: str) -> None:
        database = _build_stage0_database(tmp_path)
        request = _request(tmp_path, database, run_id=run_id)
        with pytest.raises(MigrationFailureError) as exc_info:
            migrate_existing_database(request)
        assert exc_info.value.code is MaintenanceFailureCode.INVALID_REQUEST
        assert _revision(database) == _STAGE0_REVISION

    def test_overlapping_database_and_output_roots_are_rejected(self, tmp_path: Path) -> None:
        database = _build_stage0_database(tmp_path)
        request = MigrationRequest(
            run_id="mig-overlap",
            database_path=database,
            snapshot_root=database.parent / "snapshots",
            isolated_root=tmp_path / "isolated",
            target_revision="head",
            requested_at=_NOW,
            code_version="abc123",
        )
        with pytest.raises(MigrationFailureError) as exc_info:
            migrate_existing_database(request)
        assert exc_info.value.code is MaintenanceFailureCode.INVALID_PATH

    @pytest.mark.parametrize("reparse_kind", ("symlink", "junction"))
    def test_reparse_output_root_is_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reparse_kind: str,
    ) -> None:
        database = _build_stage0_database(tmp_path)
        reparse_root = tmp_path / "snapshot-reparse"
        reparse_root.mkdir()
        original = is_reparse_point

        def detect_reparse(path: Path) -> bool:
            return path == reparse_root or original(path)

        monkeypatch.setattr("quantking.common.paths.is_reparse_point", detect_reparse)
        request = MigrationRequest(
            run_id=f"mig-{reparse_kind}",
            database_path=database,
            snapshot_root=reparse_root,
            isolated_root=tmp_path / "isolated",
            target_revision="head",
            requested_at=_NOW,
            code_version="abc123",
        )
        with pytest.raises(MigrationFailureError) as exc_info:
            migrate_existing_database(request)
        assert exc_info.value.code is MaintenanceFailureCode.INVALID_PATH


class TestMaintenanceExclusivity:
    def test_concurrent_writer_is_rejected_for_entire_pre_original_boundary(
        self, tmp_path: Path
    ) -> None:
        database = _build_stage0_database(tmp_path)
        request = _request(tmp_path, database, run_id="mig-writer-lock")
        writer_rejected = {"value": False}

        def attack_writer(_checkpoint: MigrationCheckpoint) -> None:
            with sqlite3.connect(database, timeout=0) as connection:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    connection.execute("BEGIN IMMEDIATE")
                writer_rejected["value"] = True

        result = migrate_existing_database(request, before_original_migration=attack_writer)
        assert result.ok
        assert writer_rejected["value"]

    def test_second_migration_process_is_rejected(self, tmp_path: Path) -> None:
        database = _build_stage0_database(tmp_path)
        request = _request(tmp_path, database, run_id="mig-process-lock")
        child_request = _request(
            tmp_path,
            database,
            run_id="mig-process-lock-child",
            snapshot_name="snapshots-child",
            isolated_name="isolated-child",
        )
        child_code = {"value": ""}

        def attack_process(_checkpoint: MigrationCheckpoint) -> None:
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            process = context.Process(target=_child_attempt, args=(child_request, queue))
            process.start()
            process.join(timeout=30)
            assert process.exitcode == 0
            child_code["value"] = queue.get(timeout=5)

        result = migrate_existing_database(request, before_original_migration=attack_process)
        assert result.ok
        assert child_code["value"] == MaintenanceFailureCode.MAINTENANCE_LOCKED.value


class TestDuplicateExecution:
    def test_duplicate_run_id_rejected_without_overwriting_evidence(self, tmp_path: Path) -> None:
        database = _build_stage0_database(tmp_path)
        request = _request(tmp_path, database, run_id="mig-duplicate")
        first = migrate_existing_database(request)
        artifacts = (
            first.snapshot_path,
            first.isolated_restore_path,
            first.isolated_path.parent,
            first.evidence_path,
            first.evidence_path.with_suffix(".sha256"),
        )
        evidence_before = _artifact_inventory(*artifacts)
        with pytest.raises(MigrationFailureError) as exc_info:
            migrate_existing_database(request)
        assert exc_info.value.code is MaintenanceFailureCode.RUN_ID_ALREADY_USED
        assert _artifact_inventory(*artifacts) == evidence_before

    @pytest.mark.parametrize(
        "artifact",
        (
            "snapshot",
            "isolated_restore",
            "isolated_migration",
            "success_evidence",
            "success_evidence_sha",
            "failure_evidence",
        ),
    )
    def test_any_existing_run_artifact_reserves_run_id_before_side_effects(
        self, tmp_path: Path, artifact: str
    ) -> None:
        database = _build_stage0_database(tmp_path)
        run_id = f"mig-existing-{artifact.replace('_', '-')}"
        request = _request(tmp_path, database, run_id=run_id)
        evidence = request.snapshot_root / "migration-evidence" / f"{run_id}.json"
        artifacts = {
            "snapshot": request.snapshot_root / run_id,
            "isolated_restore": request.isolated_root / run_id,
            "isolated_migration": request.isolated_root / f"{run_id}-migration",
            "success_evidence": evidence,
            "success_evidence_sha": evidence.with_suffix(".sha256"),
            "failure_evidence": request.snapshot_root / "migration-failures" / run_id,
        }
        existing = artifacts[artifact]
        if artifact in {"success_evidence", "success_evidence_sha"}:
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"immutable-existing-evidence")
        else:
            existing.mkdir(parents=True)

        with pytest.raises(MigrationFailureError) as exc_info:
            migrate_existing_database(request)

        assert exc_info.value.code is MaintenanceFailureCode.RUN_ID_ALREADY_USED
        assert _revision(database) == _STAGE0_REVISION
        assert not database.with_name(f".{database.name}.migration.lock").exists()

    def test_failure_before_snapshot_occupies_run_id_forever(self, tmp_path: Path) -> None:
        database = _build_stage0_database(tmp_path)
        request = replace(
            _request(tmp_path, database, run_id="mig-failed-used"),
            expected_snapshot_revision="forged_revision",
        )
        with pytest.raises(MigrationFailureError) as first:
            migrate_existing_database(request)
        assert first.value.code is MaintenanceFailureCode.SNAPSHOT_REVISION_MISMATCH
        assert first.value.evidence_path is not None
        evidence_before = first.value.evidence_path.read_bytes()

        with pytest.raises(MigrationFailureError) as repeated:
            migrate_existing_database(request)

        assert repeated.value.code is MaintenanceFailureCode.RUN_ID_ALREADY_USED
        assert first.value.evidence_path.read_bytes() == evidence_before
        assert _revision(database) == _STAGE0_REVISION

    def test_failed_run_id_cannot_retry_successfully(self, tmp_path: Path) -> None:
        database = _build_stage0_database(tmp_path)
        failed_request = replace(
            _request(tmp_path, database, run_id="mig-failed-retry"),
            expected_snapshot_revision="forged_revision",
        )
        with pytest.raises(MigrationFailureError) as first:
            migrate_existing_database(failed_request)
        assert first.value.code is MaintenanceFailureCode.SNAPSHOT_REVISION_MISMATCH

        corrected_request = replace(
            failed_request,
            expected_snapshot_revision=_STAGE0_REVISION,
        )
        with pytest.raises(MigrationFailureError) as repeated:
            migrate_existing_database(corrected_request)

        assert repeated.value.code is MaintenanceFailureCode.RUN_ID_ALREADY_USED
        assert _revision(database) == _STAGE0_REVISION


class TestFailureEvidenceContract:
    def test_failure_record_is_complete_current_and_redacted(self, tmp_path: Path) -> None:
        database = _build_stage0_database(tmp_path)
        request = replace(
            _request(tmp_path, database, run_id="mig-failure-contract"),
            expected_snapshot_revision="ATTACK_INPUT",
        )
        before = datetime.now(UTC)
        with pytest.raises(MigrationFailureError) as exc_info:
            migrate_existing_database(request)
        after = datetime.now(UTC)

        assert exc_info.value.evidence_path is not None
        record = json.loads(exc_info.value.evidence_path.read_text(encoding="utf-8"))
        assert set(record) == {
            "schema_version",
            "run_id",
            "error_code",
            "message",
            "failed_at",
        }
        assert record["schema_version"] == "1"
        assert record["run_id"] == request.run_id
        assert record["error_code"] == MaintenanceFailureCode.SNAPSHOT_REVISION_MISMATCH.value
        assert record["message"] == "source revision credential did not match"
        failed_at = UtcTimestamp.from_iso(record["failed_at"])
        assert before <= failed_at.value <= after
        rendered = json.dumps(record, ensure_ascii=False)
        assert str(database) not in rendered
        assert "ATTACK_INPUT" not in rendered

    def test_failure_writer_never_overwrites_an_existing_record(self, tmp_path: Path) -> None:
        directory = tmp_path / "migration-failures" / "mig-failure-writer"
        first = _write_failure(
            directory,
            "mig-failure-writer",
            MaintenanceFailureCode.SNAPSHOT_FAILED,
        )
        first_bytes = first.read_bytes()

        second = _write_failure(
            directory,
            "mig-failure-writer",
            MaintenanceFailureCode.ORIGINAL_MIGRATION_FAILED,
        )

        assert first.name == "failure.json"
        assert second.name == "failure-0.json"
        assert second != first
        assert first.read_bytes() == first_bytes
        assert json.loads(second.read_text(encoding="utf-8"))["error_code"] == (
            MaintenanceFailureCode.ORIGINAL_MIGRATION_FAILED.value
        )
