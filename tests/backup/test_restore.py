from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from quantking.common.time import UtcTimestamp
from quantking.jobs.backup import services as backup_services
from quantking.jobs.backup.models import ArtifactState, BackupError, BackupFailureCode
from quantking.jobs.backup.services import BackupService, load_snapshot_manifest, object_path

from .conftest import SyntheticSource, make_request


class NoSpace:
    def free_bytes(self, _path: Path) -> int:
        return 0


def _rewrite_complete(snapshot: Path) -> None:
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    _write_json(
        snapshot / "COMPLETE",
        {
            "artifact_kind": "local_snapshot",
            "input_sha256": hashlib.sha256((snapshot / "input.json").read_bytes()).hexdigest(),
            "manifest_sha256": hashlib.sha256(
                (snapshot / "manifest.json").read_bytes()
            ).hexdigest(),
            "restore_point_id": manifest["restore_point_id"],
            "schema_version": "1",
        },
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _resign_offsite_complete(point: Path) -> None:
    manifest = json.loads((point / "manifest.json").read_text(encoding="utf-8"))
    _write_json(
        point / "COMPLETE",
        {
            "artifact_kind": "offsite_restore_point",
            "input_sha256": hashlib.sha256((point / "input.json").read_bytes()).hexdigest(),
            "manifest_sha256": hashlib.sha256((point / "manifest.json").read_bytes()).hexdigest(),
            "replication_report_sha256": hashlib.sha256(
                (point / "replication-report.json").read_bytes()
            ).hexdigest(),
            "restore_point_id": point.name,
            "schema_version": "1",
            "source_restore_point_id": manifest["restore_point_id"],
        },
    )


def test_offsite_restore_materializes_exact_files_and_uses_immutable_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    service = BackupService()
    snapshot = service.create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )
    manifest = load_snapshot_manifest(snapshot.path)
    offsite = tmp_path / "offsite"
    service.replicate_to_offsite(snapshot.path, offsite, "offsite-001", created_at=fixed_time)
    calls: list[tuple[object, bool]] = []
    original_connect = sqlite3.connect

    def recording_connect(database, *args, **kwargs):
        calls.append((database, bool(kwargs.get("uri", False))))
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    restored = service.restore_from_offsite(
        offsite,
        "offsite-001",
        tmp_path / "restores",
        "restore-001",
        verified_at=fixed_time,
    )

    assert restored.state is ArtifactState.VERIFIED
    assert (restored.path / "VERIFIED").is_file()
    backup_services.verify_materialized_restore(restored.path)
    verified = json.loads((restored.path / "VERIFIED").read_text(encoding="utf-8"))
    assert verified["source_kind"] == "offsite"
    assert verified["source_restore_point_id"] == "offsite-001"
    assert verified["source_snapshot_id"] == manifest.restore_point_id
    assert (
        verified["source_replication_report_sha256"]
        == hashlib.sha256(
            (restored.path / "source-replication-report.json").read_bytes()
        ).hexdigest()
    )
    actual_payload = sorted(
        path.relative_to(restored.path / "payload").as_posix()
        for path in (restored.path / "payload").rglob("*")
        if path.is_file()
    )
    assert actual_payload == [entry.relative_path for entry in manifest.files]
    immutable_calls = [
        str(database) for database, uri in calls if uri and "immutable=1" in str(database)
    ]
    assert immutable_calls
    assert all("mode=ro&immutable=1" in value for value in immutable_calls)
    assert not any(
        "mode=ro" in str(database) and "immutable=1" not in str(database)
        for database, uri in calls
        if uri
    )
    database = restored.path / "payload" / "database" / "quantking.db"
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


@pytest.mark.parametrize("mode", ("missing", "tampered"))
def test_missing_or_tampered_offsite_object_preserves_failed_restore(
    mode: str,
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    service = BackupService()
    snapshot = service.create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )
    manifest = load_snapshot_manifest(snapshot.path)
    offsite = tmp_path / "offsite"
    service.replicate_to_offsite(snapshot.path, offsite, "offsite-001", created_at=fixed_time)
    victim = object_path(offsite, manifest.files[-1].sha256)
    if mode == "missing":
        victim.rename(victim.with_suffix(".missing"))
    else:
        victim.write_bytes(b"tampered")

    with pytest.raises(BackupError) as captured:
        service.restore_from_offsite(
            offsite,
            "offsite-001",
            tmp_path / "restores",
            "restore-001",
            verified_at=fixed_time,
        )

    assert captured.value.code is BackupFailureCode.INTEGRITY_FAILED
    failed = tmp_path / "restores" / "restore-001.incomplete"
    assert (failed / "failure.json").is_file()
    assert (failed / "restore-request.json").is_file()
    assert not (failed / "VERIFIED").exists()


def test_tampered_manifest_prevents_local_restore_and_preserves_failure(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    service = BackupService()
    snapshot = service.create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )
    manifest_path = snapshot.path / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["code_version"] = "tampered"
    manifest_path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(BackupError) as captured:
        service.restore_from_local(
            snapshot.path,
            tmp_path / "restores",
            "restore-001",
            verified_at=fixed_time,
        )

    assert captured.value.code is BackupFailureCode.MANIFEST_INVALID
    assert (tmp_path / "restores" / "restore-001.incomplete" / "failure.json").is_file()


def test_schema_or_alembic_mismatch_fails_before_verified(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    service = BackupService()
    snapshot = service.create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )
    manifest_path = snapshot.path / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["db_user_version"] = 999
    raw["alembic_version"] = "wrong"
    manifest_path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
    _rewrite_complete(snapshot.path)

    with pytest.raises(BackupError) as captured:
        service.restore_from_local(
            snapshot.path,
            tmp_path / "restores",
            "restore-001",
            verified_at=fixed_time,
        )

    assert captured.value.code is BackupFailureCode.SCHEMA_MISMATCH
    failed = tmp_path / "restores" / "restore-001.incomplete"
    assert (failed / "failure.json").is_file()
    assert not (failed / "VERIFIED").exists()


def test_extra_materialized_file_fails_and_existing_restore_target_is_preserved(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    snapshot = BackupService().create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )

    def add_extra(staging: Path) -> None:
        (staging / "payload" / "extra.bin").write_bytes(b"extra")

    with pytest.raises(BackupError) as captured:
        BackupService(before_restore_verify=add_extra).restore_from_local(
            snapshot.path,
            tmp_path / "restores",
            "restore-001",
            verified_at=fixed_time,
        )
    assert captured.value.code is BackupFailureCode.PAYLOAD_MISMATCH
    failed = tmp_path / "restores" / "restore-001.incomplete"
    before = {
        path.relative_to(failed).as_posix(): path.read_bytes()
        for path in failed.rglob("*")
        if path.is_file()
    }

    with pytest.raises(BackupError) as reused:
        BackupService().restore_from_local(
            snapshot.path,
            tmp_path / "restores",
            "restore-001",
            verified_at=fixed_time,
        )
    assert reused.value.code is BackupFailureCode.DESTINATION_EXISTS
    after = {
        path.relative_to(failed).as_posix(): path.read_bytes()
        for path in failed.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_restore_payload_space_is_preflighted_and_failure_is_preserved(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    snapshot = BackupService().create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )

    with pytest.raises(BackupError) as captured:
        BackupService(disk_space=NoSpace()).restore_from_local(
            snapshot.path,
            tmp_path / "restores",
            "restore-no-space",
            verified_at=fixed_time,
        )

    assert captured.value.code is BackupFailureCode.INSUFFICIENT_SPACE
    failed = tmp_path / "restores" / "restore-no-space.incomplete"
    assert (failed / "restore-request.json").is_file()
    assert (failed / "failure.json").is_file()
    assert not (failed / "payload").exists()
    assert not (failed / "VERIFIED").exists()


def test_corrupt_sqlite_with_resigned_manifest_fails_immutable_validation(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    snapshot = BackupService().create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )
    database = snapshot.path / "payload" / "database" / "quantking.db"
    database.write_bytes(b"this is deliberately not a SQLite database")
    manifest_path = snapshot.path / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    database_entry = next(
        entry for entry in raw["files"] if entry["relative_path"] == "database/quantking.db"
    )
    database_entry["size"] = database.stat().st_size
    database_entry["sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
    _rewrite_complete(snapshot.path)

    with pytest.raises(BackupError) as captured:
        BackupService().restore_from_local(
            snapshot.path,
            tmp_path / "restores",
            "restore-corrupt-db",
            verified_at=fixed_time,
        )

    assert captured.value.code is BackupFailureCode.INTEGRITY_FAILED
    failed = tmp_path / "restores" / "restore-corrupt-db.incomplete"
    assert (failed / "failure.json").is_file()
    assert not (failed / "VERIFIED").exists()
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_offsite_restore_rejects_object_path_resolution_escape(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    service = BackupService()
    snapshot = service.create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )
    manifest = load_snapshot_manifest(snapshot.path)
    offsite = tmp_path / "offsite"
    service.replicate_to_offsite(snapshot.path, offsite, "offsite-001", created_at=fixed_time)
    escaped = object_path(offsite, manifest.files[0].sha256).parent
    outside = tmp_path / "outside-object-read"
    outside.mkdir()
    original_resolve = Path.resolve

    def resolve_escape(path: Path, strict: bool = False) -> Path:
        if path == escaped:
            return outside
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_escape)
    with pytest.raises(BackupError) as captured:
        service.restore_from_offsite(
            offsite,
            "offsite-001",
            tmp_path / "restores",
            "restore-escaped-object",
            verified_at=fixed_time,
        )

    assert captured.value.code is BackupFailureCode.INVALID_PATH
    assert (tmp_path / "restores" / "restore-escaped-object.incomplete" / "failure.json").is_file()
    assert not any(outside.iterdir())


def test_offsite_restore_rejects_tampered_replication_report(
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
    point = offsite / "restore_points" / "offsite-001"
    _write_json(point / "replication-report.json", {})

    with pytest.raises(BackupError) as captured:
        service.restore_from_offsite(
            offsite,
            "offsite-001",
            tmp_path / "restores",
            "restore-report-tampered",
            verified_at=fixed_time,
        )

    assert captured.value.code is BackupFailureCode.MANIFEST_INVALID


@pytest.mark.parametrize("document", ("manifest", "input"))
def test_offsite_restore_rejects_unsupported_metadata_schema_even_when_resigned(
    document: str,
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
    point = offsite / "restore_points" / "offsite-001"
    manifest_path = point / "manifest.json"
    input_path = point / "input.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if document == "manifest":
        manifest["schema_version"] = "999"
    else:
        input_document = json.loads(input_path.read_text(encoding="utf-8"))
        input_document["schema_version"] = "999"
        _write_json(input_path, input_document)
        manifest["input_manifest_sha256"] = hashlib.sha256(input_path.read_bytes()).hexdigest()
    _write_json(manifest_path, manifest)
    _resign_offsite_complete(point)

    with pytest.raises(BackupError) as captured:
        service.restore_from_offsite(
            offsite,
            "offsite-001",
            tmp_path / "restores",
            f"restore-schema-{document}",
            verified_at=fixed_time,
        )

    assert captured.value.code is BackupFailureCode.MANIFEST_INVALID


def test_offsite_restore_rejects_report_id_that_disagrees_with_directory(
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
    point = offsite / "restore_points" / "offsite-001"
    report_path = point / "replication-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["restore_point_id"] = "different-point"
    _write_json(report_path, report)
    _resign_offsite_complete(point)

    with pytest.raises(BackupError) as captured:
        service.restore_from_offsite(
            offsite,
            "offsite-001",
            tmp_path / "restores",
            "restore-id-mismatch",
            verified_at=fixed_time,
        )

    assert captured.value.code is BackupFailureCode.MANIFEST_INVALID


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", "999"),
        ("state", "failed"),
        ("total_objects", 999),
        ("copied", 999),
        ("skipped", 999),
        ("conflicted", 1),
    ),
)
def test_offsite_restore_rejects_inconsistent_resigned_replication_report(
    field: str,
    value: object,
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
    point = offsite / "restore_points" / "offsite-001"
    report_path = point / "replication-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report[field] = value
    _write_json(report_path, report)
    _resign_offsite_complete(point)

    with pytest.raises(BackupError) as captured:
        service.restore_from_offsite(
            offsite,
            "offsite-001",
            tmp_path / "restores",
            f"restore-report-{field}",
            verified_at=fixed_time,
        )

    assert captured.value.code is BackupFailureCode.MANIFEST_INVALID


def test_offsite_completion_marker_rejects_unknown_fields(
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
    point = offsite / "restore_points" / "offsite-001"
    marker_path = point / "COMPLETE"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["unrecognized"] = "must-be-rejected"
    _write_json(marker_path, marker)

    with pytest.raises(BackupError) as captured:
        service.restore_from_offsite(
            offsite,
            "offsite-001",
            tmp_path / "restores",
            "restore-extra-marker-field",
            verified_at=fixed_time,
        )

    assert captured.value.code is BackupFailureCode.MANIFEST_INVALID


def test_materialized_restore_verification_binds_all_metadata_and_ids(
    synthetic_source: SyntheticSource,
    fixed_time: UtcTimestamp,
    tmp_path: Path,
) -> None:
    service = BackupService()
    snapshot = service.create_local_snapshot(
        make_request(synthetic_source, tmp_path / "local", fixed_time)
    )
    restored = service.restore_from_local(
        snapshot.path,
        tmp_path / "restores",
        "restore-verified",
        verified_at=fixed_time,
    )

    backup_services.verify_materialized_restore(restored.path)
    marker_path = restored.path / "VERIFIED"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["schema_version"] == "1"
    assert marker["restore_id"] == restored.path.name
    assert (
        marker["input_sha256"]
        == hashlib.sha256((restored.path / "input.json").read_bytes()).hexdigest()
    )
    assert (
        marker["source_complete_sha256"]
        == hashlib.sha256((restored.path / "source-complete.json").read_bytes()).hexdigest()
    )
    marker["unknown"] = "reject"
    _write_json(marker_path, marker)

    with pytest.raises(BackupError) as captured:
        backup_services.verify_materialized_restore(restored.path)
    assert captured.value.code is BackupFailureCode.MANIFEST_INVALID
