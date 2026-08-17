"""S1-08R2 A2/A3/A4: legal-empty idempotency, cross-process single publisher
and explicit ``.incomplete`` recovery contracts."""

from __future__ import annotations

import json
import multiprocessing
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

from quantking.common.ids import EntityId, new_entity_id
from quantking.common.time import UtcTimestamp, utc_now
from quantking.data.adapters.canonical_schema import canonical_schema
from quantking.data.adapters.warehouse.canonical_warehouse import (
    CanonicalPublishRequest,
    CanonicalWarehouse,
    CanonicalWarehouseError,
)
from quantking.data.adapters.warehouse.manifest import (
    WAREHOUSE_COMPLETE_FILENAME,
    WAREHOUSE_MANIFEST_FILENAME,
    path_safe_id,
)
from quantking.data.adapters.warehouse.publish_lock import (
    PublishInProgressError,
    VersionPublishLock,
    lock_path_for,
    recover_stale_publish_lock,
)
from quantking.data.adapters.warehouse.verifier import verify_canonical_directory
from quantking.data.models import (
    DatasetKind,
    DatasetVersion,
    EmptyResultInfo,
    LegalEmptyClassification,
)


def _enforce(dataset: str, table: pa.Table) -> pa.Table:
    return table.cast(canonical_schema(dataset))


@dataclass(frozen=True, slots=True)
class _Row:
    code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    pre_close: float
    volume: int
    amount: float


def _rows() -> list[_Row]:
    return [
        _Row("000001.SZ", date(2026, 7, 15), 10.0, 11.0, 9.5, 10.5, 10.0, 1_000_000, 1.05e7),
        _Row("000001.SZ", date(2026, 7, 16), 10.5, 10.8, 10.2, 10.7, 10.5, 1_100_000, 1.17e7),
    ]


def _table() -> pa.Table:
    return _enforce(
        "daily_bar_unadjusted",
        pa.Table.from_pylist([asdict(row) for row in _rows()]),
    )


def _empty_table() -> pa.Table:
    return _enforce(
        "daily_bar_unadjusted",
        pa.table(
            {
                "code": pa.array([], type=pa.string()),
                "trade_date": pa.array([], type=pa.date32()),
                "open": pa.array([], type=pa.float64()),
                "high": pa.array([], type=pa.float64()),
                "low": pa.array([], type=pa.float64()),
                "close": pa.array([], type=pa.float64()),
                "pre_close": pa.array([], type=pa.float64()),
                "volume": pa.array([], type=pa.int64()),
                "amount": pa.array([], type=pa.float64()),
            }
        ),
    )


def _version(version_id: EntityId | None = None) -> DatasetVersion:
    return DatasetVersion.building(
        version_id=version_id or new_entity_id("data_version"),
        dataset=DatasetKind.DAILY_BAR_UNADJUSTED,
        schema_version="daily_bar_unadjusted.v1",
        code_version="s1-08r2.v1",
        rule_version="quality-rules.daily-bar-unadjusted.k4.v1",
        parent_version_id=None,
        input_batch_ids=(),
        created_at=utc_now(),
    )


def _request(
    version: DatasetVersion,
    *,
    empty: bool = False,
) -> CanonicalPublishRequest:
    return CanonicalPublishRequest(
        version=version,
        tables=(_empty_table() if empty else _table(),),
        code_version=version.code_version,
        rule_version=version.rule_version,
        empty_result=(
            EmptyResultInfo(
                classification=LegalEmptyClassification.NO_TRADING_SESSION,
                reason="calendar is closed for the entire window",
            )
            if empty
            else None
        ),
    )


# -- A2: legal-empty idempotent convergence ---------------------------------


def test_legal_empty_version_republishes_idempotently(tmp_path: Path) -> None:
    warehouse = CanonicalWarehouse(data_root=tmp_path)
    version = _version()
    request = _request(version, empty=True)

    first = warehouse.publish(request)
    manifest_path = first / WAREHOUSE_MANIFEST_FILENAME
    first_manifest_bytes = manifest_path.read_bytes()
    first_stat = {path: path.stat() for path in first.rglob("*") if path.is_file()}

    second = warehouse.publish(request)

    assert second == first
    assert manifest_path.read_bytes() == first_manifest_bytes
    for path, stat in first_stat.items():
        assert path.stat().st_mtime_ns == stat.st_mtime_ns
        assert path.stat().st_size == stat.st_size
    manifest = json.loads(first_manifest_bytes)
    assert manifest["content_sha256"]
    assert manifest["coverage_summary"]["security_count"] == "0"
    assert manifest["code_version"] == "s1-08r2.v1"
    assert manifest["rule_version"] == "quality-rules.daily-bar-unadjusted.k4.v1"
    directories = list((tmp_path / "warehouse" / "daily_bar_unadjusted").glob("*"))
    published = [item for item in directories if item.name == path_safe_id(str(version.version_id))]
    assert len(published) == 1
    verify_canonical_directory(first, data_root=tmp_path)


def test_legal_empty_conflicting_republish_is_rejected(tmp_path: Path) -> None:
    from quantking.common.errors import IdempotencyConflictError

    warehouse = CanonicalWarehouse(data_root=tmp_path)
    version = _version()
    warehouse.publish(_request(version, empty=True))
    conflicting = CanonicalPublishRequest(
        version=version,
        tables=(_table(),),
        code_version=version.code_version,
        rule_version="quality-rules.daily-bar-unadjusted.k4.v1",
    )
    with pytest.raises((IdempotencyConflictError, CanonicalWarehouseError)):
        warehouse.publish(conflicting)


# -- A3: cross-process single publisher -------------------------------------


def test_live_lock_conflict_fails_closed_with_bounded_wait(tmp_path: Path) -> None:
    warehouse = CanonicalWarehouse(
        data_root=tmp_path,
        lock_sleeper=lambda seconds: None,
        lock_timeout_seconds=0.0,
    )
    version = _version()
    lock = VersionPublishLock(
        lock_path_for(
            tmp_path,
            "daily_bar_unadjusted",
            path_safe_id(str(version.version_id)),
        )
    )
    lock.acquire()
    try:
        with pytest.raises(PublishInProgressError):
            warehouse.publish(_request(version))
    finally:
        lock.release()
    # After the holder releases, the same publish converges idempotently.
    landed = warehouse.publish(_request(version))
    assert (landed / WAREHOUSE_COMPLETE_FILENAME).is_file()


def _spawn_publish_worker(
    data_root: Path,
    version_id: str,
    rendezvous: Path,
    worker_name: str,
) -> None:
    from quantking.common.ids import parse_entity_id

    warehouse = CanonicalWarehouse(data_root=data_root, lock_timeout_seconds=30.0)
    version = _version(parse_entity_id(version_id))
    lock = VersionPublishLock(
        lock_path_for(
            data_root,
            "daily_bar_unadjusted",
            path_safe_id(version_id),
        )
    )
    ready = rendezvous / f"ready-{worker_name}"
    ready.write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        markers = list(rendezvous.glob("ready-*"))
        if len(markers) >= 2:
            break
        time.sleep(0.01)
    try:
        lock.acquire()
        owner = True
    except PublishInProgressError:
        owner = False
    else:
        # Hold the probe lock for a generous overlap window so the other
        # spawned worker's probe (which fails immediately on conflict)
        # deterministically observes this holder instead of racing through
        # a microsecond release gap.
        time.sleep(2.0)
        lock.release()
    try:
        directory = warehouse.publish(_request(version))
        (rendezvous / f"outcome-{worker_name}.json").write_text(
            json.dumps({"status": "ok", "owner": owner, "directory": str(directory)}),
            encoding="utf-8",
        )
    except Exception as error:
        (rendezvous / f"outcome-{worker_name}.json").write_text(
            json.dumps({"status": "failed", "owner": owner, "error": type(error).__name__}),
            encoding="utf-8",
        )


def test_two_spawn_processes_publish_the_same_version_with_one_owner(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    version = _version()
    rendezvous = tmp_path / "rendezvous"
    rendezvous.mkdir()
    processes = [
        context.Process(
            target=_spawn_publish_worker,
            args=(tmp_path, str(version.version_id), rendezvous, f"w{index}"),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0
    outcomes = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(rendezvous.glob("outcome-*.json"))
    ]
    assert len(outcomes) == 2, outcomes

    owners = [outcome for outcome in outcomes if outcome["owner"]]
    assert len(owners) == 1, outcomes
    assert all(outcome["status"] == "ok" for outcome in outcomes), outcomes
    final = tmp_path / "warehouse" / "daily_bar_unadjusted" / path_safe_id(str(version.version_id))
    assert (final / WAREHOUSE_COMPLETE_FILENAME).is_file()
    verify_canonical_directory(final, data_root=tmp_path)
    leftovers = [
        item
        for item in (tmp_path / "warehouse" / "daily_bar_unadjusted").glob("*")
        if ".incomplete" in item.name
    ]
    assert leftovers == []


def test_stale_publish_lock_recovery_preserves_evidence(tmp_path: Path) -> None:
    lock_path = lock_path_for(tmp_path, "daily_bar_unadjusted", "some-version")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("pid=4242\nacquired_at=2026-08-17T00:00:00Z\n", encoding="utf-8")
    live_reference = UtcTimestamp.from_iso("2026-08-17T00:00:30Z")

    live = recover_stale_publish_lock(
        lock_path,
        stale_after=timedelta(hours=1),
        now=live_reference,
    )
    assert live is None
    assert lock_path.is_file()

    stale_reference = UtcTimestamp.from_iso("2026-08-17T02:00:00Z")
    stale = recover_stale_publish_lock(
        lock_path,
        stale_after=timedelta(hours=1),
        now=stale_reference,
    )
    assert stale is not None
    assert stale.owner == "4242"
    assert not lock_path.exists()
    assert stale.preserved_path.is_file()


# -- A4: explicit .incomplete recovery ---------------------------------------


def test_interrupted_incomplete_is_recovered_and_republished(tmp_path: Path) -> None:
    warehouse = CanonicalWarehouse(data_root=tmp_path)
    version = _version()
    final = tmp_path / "warehouse" / "daily_bar_unadjusted" / path_safe_id(str(version.version_id))
    incomplete = final.with_name(final.name + ".incomplete")
    incomplete.mkdir(parents=True)
    (incomplete / "half-written.parquet").write_bytes(b"partial")

    with pytest.raises(CanonicalWarehouseError):
        warehouse.publish(_request(version))

    outcome = warehouse.recover_stale_incomplete(version, now_iso="2026-08-17T12:00:00Z")

    assert outcome["status"] == "recovered"
    assert outcome["classification"] == "staging-interrupted"
    preserved = final.parent / str(outcome["preserved_directory"])
    assert (preserved / "half-written.parquet").read_bytes() == b"partial"
    assert not incomplete.exists()

    landed = warehouse.publish(_request(version))
    assert landed == final
    assert (final / WAREHOUSE_COMPLETE_FILENAME).is_file()
    assert preserved.is_dir()
    verify_canonical_directory(final, data_root=tmp_path)


def test_recovery_of_absent_incomplete_is_reported(tmp_path: Path) -> None:
    warehouse = CanonicalWarehouse(data_root=tmp_path)
    outcome = warehouse.recover_stale_incomplete(_version())
    assert outcome["status"] == "absent"
