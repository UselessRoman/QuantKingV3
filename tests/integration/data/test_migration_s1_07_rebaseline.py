"""The S1-07 rebaseline keeps migration history but removes the write gate."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from quantking.jobs.adapters.persistence.sqlite import _REPOSITORY_ROOT, create_sqlite_engine

HEAD = "20260808_0013"


def _config(database: Path) -> Config:
    config = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(_REPOSITORY_ROOT / "migrations").replace("%", "%%")
    )
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return config


def test_rebaseline_preserves_columns_and_removes_s1_07_write_gate(tmp_path: Path) -> None:
    database = tmp_path / "rebaseline.sqlite3"
    command.upgrade(_config(database), HEAD)
    engine = create_sqlite_engine(database, busy_timeout_ms=5_000)
    columns = {
        item["name"]: item for item in inspect(engine).get_columns("source_candidate_assessments")
    }
    assert columns["sealed_evidence_relative_path"]["nullable"] is True
    assert columns["sealed_evidence_sha256"]["nullable"] is True
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'source_assessments_r5_%'"
                )
            )
        }
    assert triggers == set()
