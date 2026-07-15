from __future__ import annotations

import ast
import inspect
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from quantking.common.time import UtcTimestamp
from quantking.jobs.backup.adapters.filesystem import (
    LocalStableFileCopier,
)
from quantking.jobs.backup.adapters.sqlite import SQLiteOnlineBackup
from quantking.jobs.backup.models import (
    ArtifactState,
    BackupError,
    BackupFailureCode,
)
from quantking.jobs.backup.services import BackupService, verify_local_snapshot

from .conftest import SyntheticSource, make_request


class NoSpace:
    def free_bytes(self, _path: Path) -> int:
        return 0


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_active_wal_database_uses_online_backup_and_contains_committed_wal_data(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    assert Path(f"{synthetic_source.database}-wal").exists()

    def forbidden_copy(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("active SQLite must never use shutil.copy/copy2")

    monkeypatch.setattr(shutil, "copy", forbidden_copy)
    monkeypatch.setattr(shutil, "copy2", forbidden_copy)
    result = BackupService().create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )

    assert result.state is ArtifactState.COMPLETE
    snapshot_db = result.path / "payload" / "database" / "quantking.db"
    uri = f"{snapshot_db.resolve().as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as restored:
        assert restored.execute("SELECT value FROM facts").fetchone() == ("committed-in-wal",)
    assert not Path(f"{snapshot_db}-wal").exists()
    assert not Path(f"{snapshot_db}-shm").exists()

    source = inspect.getsource(SQLiteOnlineBackup)
    tree = ast.parse(source)
    called_names = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "backup" in called_names
    assert "copy" not in called_names
    assert "copy2" not in called_names


def test_manifest_is_complete_sorted_and_matches_the_exact_payload_set(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    result = BackupService().create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )
    verification = verify_local_snapshot(result.path)
    manifest = verification.manifest

    assert manifest.created_at == fixed_time
    assert manifest.code_version.startswith("f076ae3")
    assert manifest.db_user_version == 7
    assert manifest.alembic_version == "s0_03_head"
    assert [entry.relative_path for entry in manifest.files] == sorted(
        entry.relative_path for entry in manifest.files
    )
    assert all("\\" not in entry.relative_path for entry in manifest.files)
    assert all(len(entry.sha256) == 64 and entry.size >= 0 for entry in manifest.files)
    actual_payload = sorted(
        path.relative_to(result.path / "payload").as_posix()
        for path in (result.path / "payload").rglob("*")
        if path.is_file()
    )
    assert actual_payload == [entry.relative_path for entry in manifest.files]
    assert (result.path / "input.json").is_file()
    assert (result.path / "manifest.json").is_file()
    assert (result.path / "COMPLETE").is_file()
    assert not any(path.name.endswith(("-wal", "-shm")) for path in result.path.rglob("*"))
    assert all("quantking.db-wal" not in item.source_relative_path for item in manifest.inputs)


def test_extra_payload_aborts_publish_and_failed_directory_is_never_reused(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "local"

    def add_extra(staging: Path) -> None:
        (staging / "payload" / "unlisted.bin").write_bytes(b"must fail")

    service = BackupService(before_snapshot_publish=add_extra)
    with pytest.raises(BackupError) as captured:
        service.create_local_snapshot(make_request(synthetic_source, local_root, fixed_time))
    assert captured.value.code is BackupFailureCode.PAYLOAD_MISMATCH
    failed = local_root / "local-001.incomplete"
    assert (failed / "failure.json").is_file()
    assert (failed / "input.json").is_file()
    assert not (failed / "COMPLETE").exists()
    before = _tree_bytes(failed)

    with pytest.raises(BackupError) as reused:
        BackupService().create_local_snapshot(
            make_request(synthetic_source, local_root, fixed_time)
        )
    assert reused.value.code is BackupFailureCode.DESTINATION_EXISTS

    BackupService().create_local_snapshot(
        make_request(synthetic_source, local_root, fixed_time, run_id="local-002")
    )
    assert _tree_bytes(failed) == before


def test_non_database_source_change_is_detected_after_copy(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    changed = False

    def mutate(source: Path) -> None:
        nonlocal changed
        if source.name == "part-a.bin" and not changed:
            source.write_bytes(b"changed-during-copy")
            changed = True

    copier = LocalStableFileCopier(after_copy=mutate)
    with pytest.raises(BackupError) as captured:
        BackupService(file_copier=copier).create_local_snapshot(
            make_request(synthetic_source, tmp_path / "local", fixed_time)
        )

    assert captured.value.code is BackupFailureCode.SOURCE_CHANGED
    failure = json.loads(
        (tmp_path / "local" / "local-001.incomplete" / "failure.json").read_text(encoding="utf-8")
    )
    assert failure["error_code"] == "source_changed"
    assert str(synthetic_source.root) not in json.dumps(failure)


def test_injected_insufficient_space_fails_stably_and_preserves_evidence(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    with pytest.raises(BackupError) as captured:
        BackupService(disk_space=NoSpace()).create_local_snapshot(
            make_request(synthetic_source, tmp_path / "local", fixed_time)
        )

    assert captured.value.code is BackupFailureCode.INSUFFICIENT_SPACE
    failed = tmp_path / "local" / "local-001.incomplete"
    assert (failed / "input.json").is_file()
    assert (failed / "failure.json").is_file()
    assert not (failed / "COMPLETE").exists()


@pytest.mark.parametrize(
    "mutate",
    (
        lambda request, tmp: request.with_paths(source_root=Path("relative-source")),
        lambda request, tmp: request.with_paths(local_backup_root=request.source_root / "backups"),
        lambda request, tmp: request.with_database_relative_path("../escape.db"),
        lambda request, tmp: request.with_run_id("../escape"),
    ),
)
def test_invalid_relative_escape_and_overlapping_paths_are_rejected(
    mutate,
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    request = make_request(synthetic_source, tmp_path / "local", fixed_time)
    with pytest.raises(BackupError) as captured:
        BackupService().create_local_snapshot(mutate(request, tmp_path))
    assert captured.value.code is BackupFailureCode.INVALID_PATH


def test_existing_completed_destination_is_rejected(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "local" / "local-001"
    destination.mkdir(parents=True)
    sentinel = destination / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(BackupError) as captured:
        BackupService().create_local_snapshot(
            make_request(synthetic_source, tmp_path / "local", fixed_time)
        )
    assert captured.value.code is BackupFailureCode.DESTINATION_EXISTS
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_legacy_manifest_only_completion_marker_is_explicitly_rejected(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    result = BackupService().create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )
    marker_path = result.path / "COMPLETE"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker_path.write_text(
        json.dumps({"manifest_sha256": marker["manifest_sha256"]}) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(BackupError) as captured:
        verify_local_snapshot(result.path)

    assert captured.value.code is BackupFailureCode.MANIFEST_INVALID


def test_symlink_escape_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    link = synthetic_source.root / "partitions" / "escape.bin"
    link.write_bytes(b"placeholder")
    original_resolve = Path.resolve

    def resolve_escape(path: Path, strict: bool = False) -> Path:
        if path == link:
            return outside
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_escape)
    request = make_request(synthetic_source, tmp_path / "local", fixed_time)
    request = request.with_files(
        (*request.files, request.files[-1].with_path("partitions/escape.bin"))
    )

    with pytest.raises(BackupError) as captured:
        BackupService().create_local_snapshot(request)
    assert captured.value.code is BackupFailureCode.INVALID_PATH
