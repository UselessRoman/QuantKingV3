from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from quantking.data.adapters.persistence.models import Base
from quantking.jobs.adapters.persistence.sqlite import initialize_database


def _insert_job(connection: sqlite3.Connection, suffix: int = 1) -> str:
    job_id = f"job:00000000-0000-4000-8000-{suffix:012d}"
    connection.execute(
        "INSERT INTO job_runs (job_id,job_type,idempotency_key,input_manifest_json,"
        "config_manifest_json,version_manifest_json,requester,status,created_at,progress,"
        "log_id,artifacts_json,row_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            "data.eod.v1",
            f"resolution-migration-{suffix}",
            "{}",
            "{}",
            "{}",
            "test",
            "FAILED",
            "2026-08-10T00:00:00Z",
            0.0,
            "logs/test.jsonl",
            "[]",
            1,
        ),
    )
    return job_id


def test_0014_adds_only_immutable_resolution_summary_storage(tmp_path: Path) -> None:
    database = tmp_path / "resolution.sqlite3"
    initialize_database(database)
    run_id = "data_resolution:00000000-0000-4000-8000-000000000002"
    with sqlite3.connect(database) as connection:
        job_id = _insert_job(connection)
        connection.execute(
            "INSERT INTO resolution_runs (run_id,dataset,created_job_id,"
            "candidate_version_id,created_at,rule_version,threshold_rule_version,"
            "input_fingerprint,artifact_relative_path,manifest_sha256,total_decisions,"
            "default_decision_count,exception_count,conflict_count,blocker_count) "
            "VALUES (?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                "daily_bar_unadjusted",
                job_id,
                "2026-08-10T00:01:00Z",
                "source-resolution.daily-bar-unadjusted.k2.v1",
                "comparison-thresholds.daily-bar-unadjusted.k1.v1",
                "a" * 64,
                "data-resolution/run",
                "b" * 64,
                1,
                0,
                0,
                1,
                0,
            ),
        )
        connection.commit()
        columns = {row[1] for row in connection.execute("PRAGMA table_info('resolution_runs')")}
        assert "decision_json" not in columns
        assert "conflict_json" not in columns
        assert "blocker_count" in columns
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE resolution_runs SET conflict_count=2 WHERE run_id=?",
                (run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM resolution_runs WHERE run_id=?", (run_id,))
        empty_candidate = "data_version:00000000-0000-4000-8000-000000000004"
        connection.execute(
            "INSERT INTO dataset_versions (version_id,dataset,status,schema_version,"
            "code_version,rule_version,parent_version_id,input_batch_ids_json,created_at,"
            "coverage_summary_json) VALUES (?,?,?,?,?,?,NULL,?,?,?)",
            (
                empty_candidate,
                "daily_bar_unadjusted",
                "BUILDING",
                "daily_bar_unadjusted.v1",
                "k2-test",
                "quality-rules.v1",
                "[]",
                "2026-08-10T00:01:30Z",
                "{}",
            ),
        )
        empty_consensus_job = _insert_job(connection, 2)
        connection.execute(
            "INSERT INTO resolution_runs (run_id,dataset,created_job_id,"
            "candidate_version_id,created_at,rule_version,threshold_rule_version,"
            "input_fingerprint,artifact_relative_path,manifest_sha256,total_decisions,"
            "default_decision_count,exception_count,conflict_count,blocker_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "data_resolution:00000000-0000-4000-8000-000000000004",
                "daily_bar_unadjusted",
                empty_consensus_job,
                empty_candidate,
                "2026-08-10T00:01:31Z",
                "source-resolution.daily-bar-unadjusted.k2.v1",
                "comparison-thresholds.daily-bar-unadjusted.k1.v1",
                "e" * 64,
                "data-resolution/empty-consensus",
                "f" * 64,
                0,
                0,
                0,
                0,
                0,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            empty_job_id = _insert_job(connection, 3)
            connection.execute(
                "INSERT INTO resolution_runs (run_id,dataset,created_job_id,"
                "candidate_version_id,created_at,rule_version,threshold_rule_version,"
                "input_fingerprint,artifact_relative_path,manifest_sha256,total_decisions,"
                "default_decision_count,exception_count,conflict_count,blocker_count) "
                "VALUES (?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "data_resolution:00000000-0000-4000-8000-000000000003",
                    "daily_bar_unadjusted",
                    empty_job_id,
                    "2026-08-10T00:02:00Z",
                    "source-resolution.daily-bar-unadjusted.k2.v1",
                    "comparison-thresholds.daily-bar-unadjusted.k1.v1",
                    "c" * 64,
                    "data-resolution/empty",
                    "d" * 64,
                    0,
                    0,
                    0,
                    0,
                    0,
                ),
            )


def test_0014_preserves_historical_override_tables(tmp_path: Path) -> None:
    database = tmp_path / "history.sqlite3"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "source_overrides",
        "source_override_applications",
        "source_override_application_attempts",
        "resolution_runs",
    } <= tables


def test_orm_create_all_matches_resolution_constraints_and_foreign_keys() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)

    checks = {item["name"] for item in inspector.get_check_constraints("resolution_runs")}
    foreign_tables = {
        item["referred_table"] for item in inspector.get_foreign_keys("resolution_runs")
    }

    assert {
        "ck_resolution_runs_daily_only",
        "ck_resolution_runs_sha_lengths",
        "ck_resolution_runs_counts",
        "ck_resolution_runs_candidate",
    } <= checks
    assert foreign_tables == {"job_runs", "dataset_versions"}
    engine.dispose()
