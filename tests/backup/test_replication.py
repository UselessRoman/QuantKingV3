from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantking.common.time import UtcTimestamp
from quantking.jobs.backup.models import BackupError, BackupFailureCode
from quantking.jobs.backup.services import (
    BackupService,
    load_snapshot_manifest,
    object_path,
)

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


def test_incremental_object_store_copies_skips_and_only_adds_changed_object(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    service = BackupService()
    local_root = tmp_path / "local"
    offsite = tmp_path / "offsite"
    first = service.create_local_snapshot(make_request(synthetic_source, local_root, fixed_time))
    first_manifest = load_snapshot_manifest(first.path)

    initial = service.replicate_to_offsite(
        first.path, offsite, "offsite-001", created_at=fixed_time
    )
    assert initial.copied == initial.total_objects == len(first_manifest.files)
    assert initial.skipped == initial.conflicted == 0

    repeated = service.replicate_to_offsite(
        first.path, offsite, "offsite-002", created_at=fixed_time
    )
    assert repeated.copied == repeated.conflicted == 0
    assert repeated.skipped == repeated.total_objects

    historical_before = _tree_bytes(offsite / "restore_points" / "offsite-001")
    sentinel = offsite / "do-not-delete.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    (synthetic_source.root / "partitions" / "part-a.bin").write_bytes(b"partition-a-v2")
    second = service.create_local_snapshot(
        make_request(synthetic_source, local_root, fixed_time, run_id="local-002")
    )
    changed = service.replicate_to_offsite(
        second.path, offsite, "offsite-003", created_at=fixed_time
    )

    assert changed.copied == 1
    assert changed.skipped == changed.total_objects - 1
    assert changed.conflicted == 0
    assert changed.copied + changed.skipped + changed.conflicted == changed.total_objects
    assert _tree_bytes(offsite / "restore_points" / "offsite-001") == historical_before
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_existing_content_address_with_wrong_bytes_conflicts_without_overwrite(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    service = BackupService()
    snapshot = service.create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )
    offsite = tmp_path / "offsite"
    service.replicate_to_offsite(snapshot.path, offsite, "offsite-001", created_at=fixed_time)
    manifest = load_snapshot_manifest(snapshot.path)
    victim = object_path(offsite, manifest.files[0].sha256)
    victim.write_bytes(b"corrupted-existing-object")
    sentinel = offsite / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(BackupError) as captured:
        service.replicate_to_offsite(snapshot.path, offsite, "offsite-002", created_at=fixed_time)

    assert captured.value.code is BackupFailureCode.OBJECT_CONFLICT
    assert victim.read_bytes() == b"corrupted-existing-object"
    failed = offsite / "restore_points" / "offsite-002.incomplete"
    report = json.loads((failed / "replication-report.json").read_text(encoding="utf-8"))
    assert report["conflicted"] == 1
    assert report["copied"] + report["skipped"] + report["conflicted"] == report["total_objects"]
    assert (failed / "failure.json").is_file()
    assert not (failed / "COMPLETE").exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_existing_restore_point_id_is_never_reused(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    service = BackupService()
    snapshot = service.create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )
    offsite = tmp_path / "offsite"
    service.replicate_to_offsite(snapshot.path, offsite, "offsite-001", created_at=fixed_time)
    before = _tree_bytes(offsite / "restore_points" / "offsite-001")

    with pytest.raises(BackupError) as captured:
        service.replicate_to_offsite(snapshot.path, offsite, "offsite-001", created_at=fixed_time)
    assert captured.value.code is BackupFailureCode.DESTINATION_EXISTS
    assert _tree_bytes(offsite / "restore_points" / "offsite-001") == before


def test_offsite_missing_object_space_is_preflighted_and_failure_is_preserved(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    snapshot = BackupService().create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )
    offsite = tmp_path / "offsite"

    with pytest.raises(BackupError) as captured:
        BackupService(disk_space=NoSpace()).replicate_to_offsite(
            snapshot.path,
            offsite,
            "offsite-no-space",
            created_at=fixed_time,
        )

    assert captured.value.code is BackupFailureCode.INSUFFICIENT_SPACE
    failed = offsite / "restore_points" / "offsite-no-space.incomplete"
    assert (failed / "failure.json").is_file()
    assert (failed / "replication-report.json").is_file()
    assert not (failed / "COMPLETE").exists()
    assert not (offsite / "objects" / "sha256").exists()


@pytest.mark.parametrize("component", ("restore_points", "object_prefix", "object_staging"))
def test_offsite_internal_path_resolution_escape_is_rejected_without_external_write(
    component: str,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    snapshot = BackupService().create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )
    manifest = load_snapshot_manifest(snapshot.path)
    offsite = tmp_path / "offsite"
    if component == "restore_points":
        escaped = offsite / "restore_points"
    elif component == "object_prefix":
        escaped = object_path(offsite, manifest.files[0].sha256).parent
    else:
        escaped = offsite / "objects" / ".incomplete"
    escaped.mkdir(parents=True)
    outside = tmp_path / f"outside-{component}"
    outside.mkdir()
    original_resolve = Path.resolve

    def resolve_escape(path: Path, strict: bool = False) -> Path:
        if path == escaped:
            return outside
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_escape)
    with pytest.raises(BackupError) as captured:
        BackupService().replicate_to_offsite(
            snapshot.path,
            offsite,
            f"offsite-{component}",
            created_at=fixed_time,
        )

    assert captured.value.code is BackupFailureCode.INVALID_PATH
    assert not any(outside.iterdir())
