"""Stage 1 data metadata migrations: new-DB head and Stage 0 -> Stage 1 upgrade."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from quantking.jobs.adapters.persistence.sqlite import (
    _REPOSITORY_ROOT,
    _reserve_new_database,
    create_sqlite_engine,
    initialize_database,
)

STAGE1_TABLES = {
    "ingestion_batches",
    "dataset_versions",
    "quality_runs",
    "difference_reports",
    "source_overrides",
}
STAGE0_TABLES = {"job_runs", "job_dependencies", "job_events", "audit_records"}


def _alembic_config(database: Path) -> Config:
    configuration = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(_REPOSITORY_ROOT / "migrations"))
    configuration.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database.as_posix()}")
    return configuration


def _upgrade(database: Path, revision: str = "head") -> None:
    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    configuration = _alembic_config(database)
    try:
        with engine.connect() as connection:
            configuration.attributes["connection"] = connection
            command.upgrade(configuration, revision)
    finally:
        engine.dispose()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_new_database_initializes_to_head_with_stage1_tables(tmp_path: Path) -> None:
    database = tmp_path / "stage1.db"
    initialize_database(database)
    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    engine.dispose()

    assert STAGE0_TABLES.issubset(table_names)
    assert STAGE1_TABLES.issubset(table_names)
    assert "alembic_version" in table_names

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert revision is not None
    assert revision[0] == "20260716_0001"


def test_stage0_revision_upgrades_to_stage1_revision(tmp_path: Path) -> None:
    database = tmp_path / "upgrade.db"
    _reserve_new_database(database)
    _upgrade(database, "20260715_0001")

    with sqlite3.connect(database) as connection:
        tables_before = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        }
        revision_before = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert STAGE0_TABLES.issubset(tables_before)
    assert STAGE1_TABLES.isdisjoint(tables_before)
    assert revision_before == ("20260715_0001",)

    _upgrade(database, "head")

    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    inspector = inspect(engine)
    tables_after = set(inspector.get_table_names())
    engine.dispose()

    assert STAGE0_TABLES.issubset(tables_after)
    assert STAGE1_TABLES.issubset(tables_after)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        revision_after = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert revision_after == ("20260716_0001",)


def test_stage1_immutable_triggers_block_published_version_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "immutable.db"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO dataset_versions "
            "(version_id, dataset, status, schema_version, code_version, "
            " rule_version, parent_version_id, input_batch_ids_json, "
            " created_at, manifest_sha256, security_count, row_count, failure_json) "
            "VALUES (?, ?, 'PUBLISHED', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                "data_version:11111111-1111-4111-8111-111111111111",
                "daily_bar_unadjusted",
                "daily_bar_unadjusted.v1",
                "abc123",
                "quality-rules.v1",
                None,
                "[]",
                "2026-07-16T01:00:00Z",
                "b" * 64,
                10,
                1000,
            ),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE dataset_versions SET status = 'BUILDING' WHERE status = 'PUBLISHED'"
            )


def test_stage1_immutable_triggers_block_applied_override_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "override.db"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO source_overrides "
            "(override_id, dataset, primary_source, fallback_source, status, "
            " scope_codes_json, scope_dates_json, fields_json, reason, "
            " created_at, approved_at, approved_by, applied_at, applied_version_id) "
            "VALUES (?, ?, ?, ?, 'APPLIED', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "data_override:22222222-2222-4222-8222-222222222222",
                "daily_bar_unadjusted",
                "XTQUANT",
                "BAOSTOCK",
                '["000001.SZ"]',
                '["2026-07-16"]',
                '["close"]',
                "missing bar",
                "2026-07-16T01:00:00Z",
                "2026-07-16T01:30:00Z",
                "analyst",
                "2026-07-16T02:00:00Z",
                "data_version:33333333-3333-4333-8333-333333333333",
            ),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE source_overrides SET status = 'PENDING' WHERE status = 'APPLIED'"
            )


def test_stage1_audit_trigger_still_blocks_update_and_delete(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO audit_records (actor, entity, action, correlation_id, "
            "occurred_at, details_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "analyst",
                "data_version:x",
                "override_approved",
                "c1",
                "2026-07-16T01:00:00Z",
                '{"a":"b"}',
            ),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE audit_records SET actor = 'evil'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM audit_records")
