"""S1-07A-R3 assessment receipt migration regression."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

_ROOT = Path(__file__).resolve().parents[3]
_PREVIOUS = "20260722_0009"
_R3 = "20260729_0010"


def _config(database: Path) -> Config:
    configuration = Config(str(_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_ROOT / "migrations"))
    configuration.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return configuration


def _columns(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(source_candidate_assessments)")
        }


def test_r3_assessment_receipt_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    database = tmp_path / "migration.db"
    configuration = _config(database)
    command.upgrade(configuration, _R3)
    expected = {"signer_profiles_json", "result_attestation_sha256s_json"}
    assert expected <= _columns(database)
    command.downgrade(configuration, _PREVIOUS)
    assert not (expected & _columns(database))
    command.upgrade(configuration, _R3)
    assert expected <= _columns(database)
