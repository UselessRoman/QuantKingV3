"""S1-04-R3 migration red tests for override application bindings."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from quantking.jobs.adapters.persistence.sqlite import _REPOSITORY_ROOT, create_sqlite_engine

HEAD = "20260721_0007"
PREVIOUS = "20260720_0006"


def _config(database: Path) -> Config:
    configuration = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_REPOSITORY_ROOT / "migrations"))
    configuration.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.as_posix()}")
    return configuration


def _run(database: Path, action: str, revision: str) -> None:
    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    configuration = _config(database)
    try:
        with engine.connect() as connection:
            configuration.attributes["connection"] = connection
            getattr(command, action)(configuration, revision)
    finally:
        engine.dispose()


def _revision(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        return str(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0])


def test_s1_04_r3_migration_upgrade_downgrade_upgrade_and_unique_override_binding(
    tmp_path: Path,
) -> None:
    database = tmp_path / "s1_04_r3.db"
    _run(database, "upgrade", "head")
    assert _revision(database) == HEAD

    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    unique_names = {
        item["name"] for item in inspector.get_unique_constraints("source_override_applications")
    }
    engine.dispose()

    assert {"source_override_applications", "source_override_application_attempts"} <= tables
    assert "uq_source_override_applications_override" in unique_names
    assert "uq_source_override_applications_child" in unique_names

    _run(database, "downgrade", PREVIOUS)
    assert _revision(database) == PREVIOUS
    _run(database, "upgrade", "head")
    assert _revision(database) == HEAD
