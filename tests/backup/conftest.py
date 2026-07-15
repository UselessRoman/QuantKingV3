from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantking.common.time import UtcTimestamp
from quantking.jobs.backup.models import (
    BackupCategory,
    LocalSnapshotRequest,
    SourceSpec,
)


@dataclass(slots=True)
class SyntheticSource:
    root: Path
    database: Path
    connection: sqlite3.Connection


@pytest.fixture
def fixed_time() -> UtcTimestamp:
    return UtcTimestamp(datetime(2026, 7, 15, 8, 0, tzinfo=UTC))


@pytest.fixture
def synthetic_source(tmp_path: Path) -> Iterator[SyntheticSource]:
    root = tmp_path / "source"
    root.mkdir()
    database = root / "quantking.db"
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("PRAGMA user_version=7")
    connection.execute("CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO alembic_version VALUES ('s0_03_head')")
    connection.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO facts(value) VALUES ('committed-in-wal')")
    connection.commit()

    files = {
        "config/app.toml": b"[app]\nname='synthetic'\n",
        "manifests/dataset.json": b'{"dataset":"synthetic"}\n',
        "partitions/part-a.bin": b"partition-a-v1",
        "partitions/part-b.bin": b"partition-b-v1",
    }
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    yield SyntheticSource(root=root, database=database, connection=connection)
    connection.close()


def make_request(
    source: SyntheticSource,
    local_root: Path,
    fixed_time: UtcTimestamp,
    *,
    run_id: str = "local-001",
    db_user_version: int = 7,
    alembic_version: str = "s0_03_head",
) -> LocalSnapshotRequest:
    return LocalSnapshotRequest(
        run_id=run_id,
        source_root=source.root,
        local_backup_root=local_root,
        database_relative_path="quantking.db",
        files=(
            SourceSpec("config/app.toml", BackupCategory.CONFIG),
            SourceSpec("manifests/dataset.json", BackupCategory.MANIFEST),
            SourceSpec("partitions/part-a.bin", BackupCategory.PARTITION),
            SourceSpec("partitions/part-b.bin", BackupCategory.PARTITION),
        ),
        created_at=fixed_time,
        code_version="f076ae37932b73e7581b4ec825a42c69ac01549c",
        db_user_version=db_user_version,
        alembic_version=alembic_version,
    )
