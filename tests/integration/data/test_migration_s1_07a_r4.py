"""S1-07A-R4 assessment persistence gate migration regression."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

_ROOT = Path(__file__).resolve().parents[3]
_R3 = "20260729_0010"
_R4 = "20260729_0011"
_TRIGGERS = {
    "source_assessments_s1_07a_insert_gate",
    "source_assessments_s1_07a_immutable_update",
    "source_assessments_s1_07a_immutable_delete",
}


def _config(database: Path) -> Config:
    configuration = Config(str(_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_ROOT / "migrations"))
    configuration.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return configuration


def _triggers(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }


def test_r4_assessment_gate_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    database = tmp_path / "migration.db"
    configuration = _config(database)
    command.upgrade(configuration, _R4)
    assert _triggers(database) >= _TRIGGERS
    command.downgrade(configuration, _R3)
    assert not (_TRIGGERS & _triggers(database))
    command.upgrade(configuration, _R4)
    assert _triggers(database) >= _TRIGGERS
