from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect

from quantking.common.errors import InvalidPathError
from quantking.common.paths import is_reparse_point
from quantking.jobs.adapters.persistence import sqlite as sqlite_adapter
from quantking.jobs.adapters.persistence.sqlite import create_sqlite_engine, initialize_database


def _ensure_link(link: Path, target: Path) -> None:
    """Create ``link`` as a symlink or junction to ``target``, or skip the test."""
    with contextlib.suppress(OSError):
        link.symlink_to(target, target_is_directory=True)
    if not os.path.lexists(link):
        junction = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if junction.returncode != 0:
            pytest.skip("symlink and junction creation unavailable")
    if not is_reparse_point(link):
        pytest.skip("link was not created as a reparse point")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_initialization_rejects_existing_user_database_without_modifying_it(
    tmp_path: Path,
) -> None:
    database = tmp_path / "user.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE user_positions (symbol TEXT PRIMARY KEY, shares INTEGER)")
        connection.execute("INSERT INTO user_positions VALUES ('600000.SH', 100)")

    before_hash = _sha256(database)
    before_files = {item.name for item in tmp_path.iterdir()}
    rejection: InvalidPathError | None = None
    try:
        initialize_database(database)
    except InvalidPathError as error:
        rejection = error

    with sqlite3.connect(database) as connection:
        after_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
        positions = connection.execute("SELECT symbol, shares FROM user_positions").fetchall()

    assert rejection is not None, (
        "existing database was migrated in place without a verified backup; "
        f"tables now present: {sorted(after_tables)}"
    )
    assert after_tables == {"user_positions"}
    assert positions == [("600000.SH", 100)]
    assert _sha256(database) == before_hash
    assert {item.name for item in tmp_path.iterdir()} == before_files


def test_initialization_rejects_an_existing_empty_file(tmp_path: Path) -> None:
    database = tmp_path / "empty.db"
    database.touch()

    with pytest.raises(InvalidPathError, match="must not already exist"):
        initialize_database(database)

    assert database.read_bytes() == b""


def test_initialization_rejects_relative_or_missing_parent_paths(tmp_path: Path) -> None:
    relative = Path("relative.db")
    missing_parent_database = tmp_path / "missing" / "jobs.db"

    with pytest.raises(InvalidPathError, match="absolute"):
        initialize_database(relative)
    with pytest.raises(InvalidPathError, match="parent must already exist"):
        initialize_database(missing_parent_database)

    assert not relative.exists()
    assert not missing_parent_database.exists()


def test_initialization_rejects_a_symlink_or_junction_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked"
    _ensure_link(linked_parent, outside)

    with pytest.raises(InvalidPathError, match="symbolic link or junction"):
        initialize_database(linked_parent / "jobs.db")

    assert not (outside / "jobs.db").exists()


def test_initialization_reparse_guard_is_exercised_without_platform_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_parent = tmp_path / "managed"
    managed_parent.mkdir()
    database = managed_parent / "jobs.db"
    monkeypatch.setattr(
        sqlite_adapter,
        "is_reparse_point",
        lambda path: path == managed_parent,
    )

    with pytest.raises(InvalidPathError, match="symbolic link or junction"):
        initialize_database(database)

    assert not database.exists()


def test_failed_alembic_initialization_is_preserved_and_cannot_be_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "failed-initialization.db"
    attempted_revisions: list[str] = []

    def fail_after_partial_schema(configuration: Any, revision: str) -> None:
        attempted_revisions.append(revision)
        connection = configuration.attributes["connection"]
        connection.exec_driver_sql("CREATE TABLE partial_failure_evidence (id INTEGER)")
        connection.commit()
        raise RuntimeError("synthetic Alembic failure")

    monkeypatch.setattr(sqlite_adapter.command, "upgrade", fail_after_partial_schema)

    with pytest.raises(RuntimeError, match="synthetic Alembic failure"):
        initialize_database(database)

    assert database.is_file()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'partial_failure_evidence'"
        ).fetchone() == ("partial_failure_evidence",)
    failed_hash = _sha256(database)

    with pytest.raises(InvalidPathError, match="must not already exist"):
        initialize_database(database)

    assert _sha256(database) == failed_hash
    assert attempted_revisions == ["head"]


def test_invalid_connection_settings_do_not_reserve_a_database(tmp_path: Path) -> None:
    database = tmp_path / "invalid-settings.db"

    with pytest.raises(ValueError, match="busy_timeout_ms"):
        initialize_database(database, busy_timeout_ms=0)

    assert not database.exists()


def test_new_temporary_database_initializes_to_head_with_required_schema(tmp_path: Path) -> None:
    database = tmp_path / "jobs.db"
    initialize_database(database)
    engine = create_sqlite_engine(database, busy_timeout_ms=250)

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert {
        "alembic_version",
        "audit_records",
        "job_dependencies",
        "job_events",
        "job_runs",
    }.issubset(table_names)
    assert "logs" not in table_names
    assert {index["name"] for index in inspector.get_indexes("job_events")} >= {
        "ix_job_events_job_event",
    }
    assert {item["name"] for item in inspector.get_unique_constraints("job_runs")} >= {
        "uq_job_runs_type_idempotency",
    }
    engine.dispose()

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()


def test_every_sqlite_engine_connection_enables_required_pragmas(tmp_path: Path) -> None:
    database = tmp_path / "pragmas.db"
    initialize_database(database)
    engine = create_sqlite_engine(database, busy_timeout_ms=250)

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 250

    engine.dispose()
