"""Explicit real-root acceptance runner using synthetic data only."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

from quantking.common.time import UtcTimestamp, utc_now
from quantking.jobs.backup.models import (
    BackupCategory,
    BackupError,
    BackupFailureCode,
    LocalSnapshotRequest,
    ReplicationReport,
    SourceSpec,
)
from quantking.jobs.backup.services import BackupService, verify_local_snapshot


def run_synthetic_acceptance(
    data_root: Path,
    offsite_root: Path,
    run_id: str,
) -> dict[str, object]:
    """Create only new run-scoped synthetic files and exercise the complete service."""
    if (
        not data_root.is_absolute()
        or not offsite_root.is_absolute()
        or not run_id
        or len(run_id) > 40
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in run_id
        )
    ):
        raise BackupError(
            BackupFailureCode.INVALID_PATH,
            "real acceptance roots and run ID must be explicit and safe",
        )
    data_run = data_root / "acceptance" / run_id
    offsite_run = offsite_root / "acceptance" / run_id
    if data_run.exists() or offsite_run.exists():
        raise BackupError(
            BackupFailureCode.DESTINATION_EXISTS,
            "real acceptance run directories already exist",
        )

    source = data_run / "source"
    local = data_run / "local-snapshots"
    restores = data_run / "isolated-restores"
    offsite_store = offsite_run / "store"
    data_run.mkdir(parents=True)
    try:
        source.mkdir()
        _seed_preservation_artifacts(offsite_store)
        preservation_before = _tree_inventory(offsite_store)
        database = source / "quantking.db"
        created_at = utc_now()
        connection = sqlite3.connect(database)
        try:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("PRAGMA user_version=1")
            connection.execute("CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO alembic_version VALUES ('synthetic_acceptance_v1')")
            connection.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO facts(value) VALUES ('synthetic-only')")
            connection.commit()
            _write_synthetic_files(source)

            service = BackupService()
            first = service.create_local_snapshot(
                _acceptance_request(source, local, f"{run_id}-local-1", created_at)
            )
            verify_local_snapshot(first.path)
            local_restore = service.restore_from_local(
                first.path,
                restores,
                f"{run_id}-restore-local",
                verified_at=utc_now(),
            )
            initial = service.replicate_to_offsite(
                first.path,
                offsite_store,
                f"{run_id}-offsite-1",
                created_at=utc_now(),
            )
            repeated = service.replicate_to_offsite(
                first.path,
                offsite_store,
                f"{run_id}-offsite-2",
                created_at=utc_now(),
            )
            (source / "partitions" / "part-a.bin").write_bytes(b"synthetic-partition-a-v2")
            second = service.create_local_snapshot(
                _acceptance_request(source, local, f"{run_id}-local-2", utc_now())
            )
            changed = service.replicate_to_offsite(
                second.path,
                offsite_store,
                f"{run_id}-offsite-3",
                created_at=utc_now(),
            )
            offsite_restore = service.restore_from_offsite(
                offsite_store,
                f"{run_id}-offsite-3",
                restores,
                f"{run_id}-restore-offsite",
                verified_at=utc_now(),
            )
        finally:
            connection.close()

        _validate_replication_reports(initial, repeated, changed)
        preservation_after = _tree_inventory(offsite_store)
        preservation_audit = _build_preservation_audit(
            preservation_before,
            preservation_after,
        )
        if not preservation_audit["zero_deletes"] or not preservation_audit["zero_modifications"]:
            raise BackupError(
                BackupFailureCode.INTEGRITY_FAILED,
                "synthetic acceptance changed or deleted pre-existing run-scoped evidence",
            )
        evidence: dict[str, object] = {
            "status": "passed",
            "run_id": run_id,
            "code_version": _code_version(),
            "local_snapshot_1": str(first.path),
            "local_snapshot_2": str(second.path),
            "local_restore": str(local_restore.path),
            "offsite_restore": str(offsite_restore.path),
            "initial": initial.to_dict(),
            "repeated": repeated.to_dict(),
            "changed": changed.to_dict(),
            "zero_deletes": preservation_audit["zero_deletes"],
            "preservation_audit": preservation_audit,
            "synthetic_data_only": True,
        }
        _write_acceptance_json(data_run / "acceptance-result.json", evidence)
    except BackupError as error:
        _preserve_acceptance_failure(data_run, error)
        raise
    except Exception as error:
        converted = BackupError(
            BackupFailureCode.IO_FAILED,
            "synthetic acceptance setup or service failed",
        )
        _preserve_acceptance_failure(data_run, converted)
        raise converted from error

    return evidence


def _acceptance_request(
    source: Path,
    local: Path,
    snapshot_id: str,
    created_at: UtcTimestamp,
) -> LocalSnapshotRequest:
    return LocalSnapshotRequest(
        run_id=snapshot_id,
        source_root=source,
        local_backup_root=local,
        database_relative_path="quantking.db",
        files=(
            SourceSpec("config/app.toml", BackupCategory.CONFIG),
            SourceSpec("manifests/dataset.json", BackupCategory.MANIFEST),
            SourceSpec("partitions/part-a.bin", BackupCategory.PARTITION),
            SourceSpec("partitions/part-b.bin", BackupCategory.PARTITION),
        ),
        created_at=created_at,
        code_version=_code_version(),
        db_user_version=1,
        alembic_version="synthetic_acceptance_v1",
    )


def _write_synthetic_files(source: Path) -> None:
    files = {
        "config/app.toml": b"[app]\nname='synthetic-acceptance'\n",
        "manifests/dataset.json": b'{"dataset":"synthetic-acceptance"}\n',
        "partitions/part-a.bin": b"synthetic-partition-a-v1",
        "partitions/part-b.bin": b"synthetic-partition-b-v1",
    }
    for relative, payload in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(payload)


def _seed_preservation_artifacts(offsite_store: Path) -> None:
    files = {
        "do-not-delete.txt": b"synthetic unrelated sentinel\n",
        "restore_points/synthetic-history/HISTORY": b"synthetic historical restore point\n",
        "restore_points/synthetic-failed.incomplete/failure.json": (
            b'{"state":"failed","synthetic":true}\n'
        ),
    }
    for relative, payload in files.items():
        path = offsite_store / Path(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())


def _tree_inventory(root: Path) -> dict[str, dict[str, object]]:
    inventory: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        payload = path.read_bytes()
        inventory[relative] = {
            "relative_path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    return inventory


def _build_preservation_audit(
    before: dict[str, dict[str, object]],
    after: dict[str, dict[str, object]],
) -> dict[str, object]:
    before_paths = set(before)
    after_paths = set(after)
    added = [after[path] for path in sorted(after_paths - before_paths)]
    deleted = [before[path] for path in sorted(before_paths - after_paths)]
    modified = [
        {
            "relative_path": path,
            "before": before[path],
            "after": after[path],
        }
        for path in sorted(before_paths & after_paths)
        if before[path] != after[path]
    ]
    preserved = [
        before[path] for path in sorted(before_paths & after_paths) if before[path] == after[path]
    ]
    return {
        "scope": "offsite_run/store",
        "before": [before[path] for path in sorted(before)],
        "after": [after[path] for path in sorted(after)],
        "added": added,
        "deleted": deleted,
        "modified": modified,
        "preserved": preserved,
        "before_file_count": len(before),
        "after_file_count": len(after),
        "added_file_count": len(added),
        "deleted_file_count": len(deleted),
        "modified_file_count": len(modified),
        "preserved_file_count": len(preserved),
        "zero_deletes": not deleted,
        "zero_modifications": not modified,
    }


def _preserve_acceptance_failure(data_run: Path, error: BackupError) -> None:
    failure = data_run / "acceptance-failure.json"
    if failure.exists():
        return
    _write_acceptance_json(
        failure,
        {
            "error_code": error.code.value,
            "failed_at": utc_now().to_iso(),
            "message": error.message,
            "status": "failed",
        },
    )


def _write_acceptance_json(path: Path, value: dict[str, object]) -> None:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _validate_replication_reports(
    initial: ReplicationReport,
    repeated: ReplicationReport,
    changed: ReplicationReport,
) -> None:
    if (
        initial.copied != initial.total_objects
        or initial.skipped != 0
        or initial.conflicted != 0
        or repeated.copied != 0
        or repeated.skipped != repeated.total_objects
        or repeated.conflicted != 0
        or changed.copied != 1
        or changed.skipped != changed.total_objects - 1
        or changed.conflicted != 0
    ):
        raise BackupError(
            BackupFailureCode.INTEGRITY_FAILED,
            "synthetic incremental replication counts did not match the expected result",
        )


def _code_version() -> str:
    repository = Path(__file__).resolve().parents[4]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BackupError(
            BackupFailureCode.IO_FAILED,
            "Git code version could not be determined for acceptance",
        ) from error
    version = result.stdout.strip()
    if not version:
        raise BackupError(
            BackupFailureCode.IO_FAILED,
            "Git code version is empty for acceptance",
        )
    return version
