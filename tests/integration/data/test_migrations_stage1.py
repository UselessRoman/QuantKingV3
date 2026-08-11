"""Stage 1 R2 metadata migrations from a new DB and the Stage 0 revision."""

from __future__ import annotations

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
    "dataset_version_batches",
    "quality_runs",
    "quality_results",
    "difference_reports",
    "source_overrides",
    "resolution_runs",
}
STAGE0_TABLES = {"job_runs", "job_dependencies", "job_events", "audit_records"}
HEAD = "20260810_0014"


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


def _revision(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def test_new_database_initializes_to_r2_head_with_lineage_tables(tmp_path: Path) -> None:
    database = tmp_path / "stage1.db"
    initialize_database(database)
    engine = create_sqlite_engine(database, busy_timeout_ms=250)
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    version_fks = inspector.get_foreign_keys("dataset_version_batches")
    override_fks = inspector.get_foreign_keys("source_overrides")
    batch_unique = inspector.get_unique_constraints("ingestion_batches")
    engine.dispose()

    assert STAGE0_TABLES | STAGE1_TABLES | {"alembic_version"} <= table_names
    assert {fk["referred_table"] for fk in version_fks} == {
        "dataset_versions",
        "ingestion_batches",
    }
    assert {fk["referred_table"] for fk in override_fks} >= {
        "difference_reports",
        "ingestion_batches",
        "dataset_versions",
    }
    assert "uq_batches_source_dataset_params" not in {item["name"] for item in batch_unique}
    assert _revision(database) == HEAD


def test_stage0_revision_upgrades_to_r2_head(tmp_path: Path) -> None:
    database = tmp_path / "upgrade.db"
    _reserve_new_database(database)
    _upgrade(database, "20260715_0001")
    assert _revision(database) == "20260715_0001"
    _upgrade(database)
    assert _revision(database) == HEAD
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert tables >= STAGE0_TABLES | STAGE1_TABLES


def test_same_request_parameters_allow_distinct_retry_batch_ids(tmp_path: Path) -> None:
    database = tmp_path / "retry.db"
    initialize_database(database)
    row = (
        "XTQUANT",
        "daily_bar_unadjusted",
        "FAILED",
        "adapter.v1",
        '{"code":"000001.SZ"}',
        "a" * 64,
        "2026-07-16T01:00:00Z",
        "2026-07-16T01:01:00Z",
        None,
        None,
        "[]",
        '{"error_type":"NETWORK_FAILURE","message":"failed","retryable":true}',
        None,
        None,
    )
    with sqlite3.connect(database) as connection:
        sql = (
            "INSERT INTO ingestion_batches (batch_id, source, dataset, status, "
            "adapter_version, request_params_json, request_params_sha256, started_at, ended_at, "
            "row_count, security_count, manifest_files_json, failure_json, empty_result_json, "
            "job_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        connection.execute(sql, ("data_batch:11111111-1111-4111-8111-111111111111", *row))
        connection.execute(sql, ("data_batch:22222222-2222-4222-8222-222222222222", *row))
        connection.commit()
        count = connection.execute("SELECT COUNT(*) FROM ingestion_batches").fetchone()
    assert count == (2,)


def test_terminal_batch_trigger_rejects_same_status_content_mutation(tmp_path: Path) -> None:
    database = tmp_path / "immutable.db"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO ingestion_batches (batch_id, source, dataset, status, "
            "adapter_version, request_params_json, request_params_sha256, started_at, ended_at, "
            "row_count, security_count, manifest_files_json, failure_json, empty_result_json) "
            "VALUES (?, 'XTQUANT', 'daily_bar_unadjusted', 'SUCCEEDED', 'v1', '{}', ?, ?, ?, "
            "1, 1, '[]', NULL, NULL)",
            (
                "data_batch:33333333-3333-4333-8333-333333333333",
                "b" * 64,
                "2026-07-16T01:00:00Z",
                "2026-07-16T01:01:00Z",
            ),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE ingestion_batches SET row_count=2 WHERE status='SUCCEEDED'")


def test_append_only_audit_trigger_remains_active(tmp_path: Path) -> None:
    database = tmp_path / "audit.db"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO audit_records (actor, entity, action, correlation_id, "
            "occurred_at, details_json) VALUES ('a','e','x','c','2026-07-16T01:00:00Z','{}')"
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM audit_records")


# ---------------------------------------------------------------------------
# S1-04: quality gate migration head and trigger enforcement
# ---------------------------------------------------------------------------


def test_new_database_initializes_to_quality_gate_head(tmp_path: Path) -> None:
    database = tmp_path / "s1_04.db"
    initialize_database(database)
    assert _revision(database) == HEAD


def test_stage0_revision_upgrades_to_quality_gate_head(tmp_path: Path) -> None:
    database = tmp_path / "s1_04_upgrade.db"
    _reserve_new_database(database)
    _upgrade(database, "20260715_0001")
    _upgrade(database, "20260716_0003")
    assert _revision(database) == "20260716_0003"
    _upgrade(database)
    assert _revision(database) == HEAD


def test_stage1_r2_revision_upgrades_to_quality_gate_head(tmp_path: Path) -> None:
    database = tmp_path / "s1_04_from_r2.db"
    _reserve_new_database(database)
    _upgrade(database, "20260716_0003")
    _upgrade(database)
    assert _revision(database) == HEAD


def test_quality_gate_trigger_blocks_published_without_passed_quality_run(
    tmp_path: Path,
) -> None:
    """A direct SQL UPDATE to PUBLISHED must raise when no PASSED run exists."""
    database = tmp_path / "gate.db"
    initialize_database(database)
    version_id = "data_version:55555555-5555-4555-8555-555555555555"
    run_id = "data_quality:77777777-7777-4777-8777-777777777777"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO dataset_versions (version_id, dataset, status, "
            "schema_version, code_version, rule_version, parent_version_id, "
            "input_batch_ids_json, created_at, validated_at, published_at, "
            "manifest_sha256, security_count, row_count, coverage_start, "
            "coverage_end, coverage_summary_json, failure_json, quality_run_id, "
            "content_sha256) "
            "VALUES (?, ?, 'VALIDATED', ?, ?, ?, NULL, '[]', ?, ?, NULL, "
            "?, 1, 1, '2026-07-15', '2026-07-15', "
            '\'{"date_count":"1","row_count":"1",'
            '"security_count":"1","first_date":"2026-07-15",'
            '"last_date":"2026-07-15"}\', NULL, NULL, NULL)',
            (
                version_id,
                "daily_bar_unadjusted",
                "daily_bar_unadjusted.v1",
                "s1-04",
                "quality-rules.v1",
                "2026-07-20T00:00:00Z",
                "2026-07-20T00:01:00Z",
                "a" * 64,
            ),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE dataset_versions SET quality_run_id=?, content_sha256=?, "
                "status='PUBLISHED', "
                "published_at='2026-07-20T00:02:00Z' WHERE version_id=?",
                (run_id, "a" * 64, version_id),
            )


def test_quality_gate_trigger_allows_published_with_passed_quality_run(
    tmp_path: Path,
) -> None:
    """A direct SQL UPDATE to PUBLISHED must succeed when a PASSED run matches."""
    database = tmp_path / "gate_pass.db"
    initialize_database(database)
    version_id = "data_version:66666666-6666-4666-8666-666666666666"
    run_id = "data_quality:77777777-7777-4777-8777-777777777777"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO dataset_versions (version_id, dataset, status, "
            "schema_version, code_version, rule_version, parent_version_id, "
            "input_batch_ids_json, created_at, validated_at, published_at, "
            "manifest_sha256, security_count, row_count, coverage_start, "
            "coverage_end, coverage_summary_json, failure_json, quality_run_id, "
            "content_sha256) "
            "VALUES (?, ?, 'VALIDATED', ?, ?, ?, NULL, '[]', ?, ?, NULL, "
            "?, 1, 1, '2026-07-15', '2026-07-15', "
            '\'{"date_count":"1","row_count":"1",'
            '"security_count":"1","first_date":"2026-07-15",'
            '"last_date":"2026-07-15"}\', NULL, NULL, NULL)',
            (
                version_id,
                "daily_bar_unadjusted",
                "daily_bar_unadjusted.v1",
                "s1-04",
                "quality-rules.v1",
                "2026-07-20T00:00:00Z",
                "2026-07-20T00:01:00Z",
                "a" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO quality_runs (run_id, version_id, status, rule_version, "
            "schema_version, content_sha256, "
            "started_at, ended_at, results_json, summary_report_relative_path, "
            "failure_json) VALUES (?, ?, 'PASSED', ?, ?, ?, ?, ?, "
            '\'[{"rule_code":"schema_conformance","passed":1,'
            '"affected_count":0,"summary":"ok",'
            '"report_relative_path":null}]\', NULL, NULL)',
            (
                run_id,
                version_id,
                "quality-rules.v1",
                "daily_bar_unadjusted.v1",
                "a" * 64,
                "2026-07-20T00:00:30Z",
                "2026-07-20T00:00:40Z",
            ),
        )
        connection.commit()
        # Should succeed because a PASSED quality run exists.
        connection.execute(
            "UPDATE dataset_versions SET quality_run_id=?, content_sha256=?, status='PUBLISHED', "
            "published_at='2026-07-20T00:02:00Z' WHERE version_id=?",
            (run_id, "a" * 64, version_id),
        )
        connection.commit()
        row = connection.execute(
            "SELECT status FROM dataset_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        assert row == ("PUBLISHED",)
