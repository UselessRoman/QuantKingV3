from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import inspect

from quantking.jobs.adapters.persistence.sqlite import create_sqlite_engine, upgrade_database


def test_empty_temporary_database_upgrades_to_head_with_required_schema(tmp_path: Path) -> None:
    database = tmp_path / "jobs.db"
    upgrade_database(database)
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
    upgrade_database(database)
    engine = create_sqlite_engine(database, busy_timeout_ms=250)

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 250

    engine.dispose()
