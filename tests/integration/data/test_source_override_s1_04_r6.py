"""S1-04-R6 migration and direct-SQL exact-scope regressions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.integration.data.test_source_override_s1_04_r5 import (
    PRE_R5_HEAD,
    R5_HEAD,
    _r5_context,
    _run_migration,
)

from quantking.common.time import utc_now
from quantking.data.adapters.persistence.maintenance import (
    MaintenanceFailureCode,
    MigrationFailureError,
    MigrationRequest,
    migrate_existing_database,
)

R6_HEAD = "20260722_0009"
SOURCE_TRIGGERS = {
    "source_overrides_validate_insert",
    "source_overrides_pending_transition",
    "source_overrides_approved_transition",
    "source_overrides_no_update_terminal",
    "source_overrides_no_delete_decided",
}
APPLICATION_TRIGGERS = {
    "source_override_applications_validate_insert",
    "source_override_applications_validate_update",
    "source_override_applications_freeze_terminal_update",
    "source_override_applications_freeze_terminal_delete",
    "source_override_attempts_validate_insert",
    "source_override_attempts_validate_update",
    "source_override_attempts_freeze_terminal_update",
    "source_override_attempts_freeze_terminal_delete",
    "source_overrides_require_published_application",
}


def _revision(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row is not None
    return str(row[0])


def _triggers(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'source_override%'"
            )
        }


def _clone_pending(database: Path, source_override_id: str, pending_override_id: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO source_overrides (override_id,dataset,difference_report_id,"
            "primary_batch_id,fallback_batch_id,primary_source,fallback_source,status,"
            "scope_codes_json,scope_dates_json,fields_json,field_replacements_json,"
            "fallback_only_keys_json,exact_scope_sha256,contribution_scope_sha256,reason,"
            "created_at,approved_at,approved_by,applied_at,applied_version_id) "
            "SELECT ?,dataset,difference_report_id,primary_batch_id,fallback_batch_id,"
            "primary_source,fallback_source,'PENDING',scope_codes_json,scope_dates_json,"
            "fields_json,field_replacements_json,fallback_only_keys_json,exact_scope_sha256,"
            "contribution_scope_sha256,reason,created_at,NULL,NULL,NULL,NULL "
            "FROM source_overrides WHERE override_id=?",
            (pending_override_id, source_override_id),
        )
        connection.commit()


def test_r6_complete_upgrade_downgrade_cycle_restores_trigger_sets(tmp_path: Path) -> None:
    database = tmp_path / "cycle.db"
    _run_migration(database, "upgrade", R5_HEAD)
    assert _triggers(database) >= SOURCE_TRIGGERS | APPLICATION_TRIGGERS

    _run_migration(database, "downgrade", PRE_R5_HEAD)
    assert _triggers(database) >= SOURCE_TRIGGERS
    assert not (_triggers(database) & APPLICATION_TRIGGERS)

    _run_migration(database, "upgrade", R5_HEAD)
    _run_migration(database, "upgrade", R6_HEAD)
    assert _triggers(database) >= SOURCE_TRIGGERS | APPLICATION_TRIGGERS

    _run_migration(database, "downgrade", R5_HEAD)
    _run_migration(database, "downgrade", PRE_R5_HEAD)
    _run_migration(database, "upgrade", R5_HEAD)
    _run_migration(database, "upgrade", R6_HEAD)
    assert _revision(database) == R6_HEAD
    assert _triggers(database) >= SOURCE_TRIGGERS | APPLICATION_TRIGGERS


def test_0008_to_0007_restores_behavior_not_only_names(tmp_path: Path) -> None:
    case = tmp_path / "behavior_0007"
    case.mkdir()
    ctx = _r5_context(case)
    database = case / "data.db"
    pending_id = "data_override:11111111-1111-4111-8111-111111111111"
    _clone_pending(database, str(ctx.override_id), pending_id)
    ctx.engine.dispose()
    _run_migration(database, "downgrade", PRE_R5_HEAD)

    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO source_overrides (override_id,dataset,difference_report_id,"
                "primary_batch_id,fallback_batch_id,primary_source,fallback_source,status,"
                "scope_codes_json,scope_dates_json,fields_json,reason,created_at) "
                "VALUES ('data_override:22222222-2222-4222-8222-222222222222',"
                "'daily_bar_unadjusted','data_diff:22222222-2222-4222-8222-222222222222',"
                "'data_batch:22222222-2222-4222-8222-222222222222',"
                "'data_batch:33333333-3333-4333-8333-333333333333','EASTMONEY','TENCENT',"
                "'PENDING','[\"000001.SZ\"]','[\"2026-07-15\"]','[\"close\"]','x',"
                "'2026-07-22T00:00:00Z')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE source_overrides SET status='APPROVED',reason='changed',"
                "approved_at='2026-07-22T00:00:00Z',approved_by='analyst' WHERE override_id=?",
                (pending_id,),
            )
        connection.execute(
            "UPDATE source_overrides SET status='REJECTED',"
            "approved_at='2026-07-22T00:00:00Z',approved_by='analyst' WHERE override_id=?",
            (pending_id,),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE source_overrides SET reason='changed' WHERE override_id=?", (pending_id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM source_overrides WHERE override_id=?", (pending_id,))


def test_0009_rejects_exact_scope_changes_during_both_decisions(tmp_path: Path) -> None:
    case = tmp_path / "behavior_0009"
    case.mkdir()
    ctx = _r5_context(case)
    database = case / "data.db"
    pending_id = "data_override:44444444-4444-4444-8444-444444444444"
    _clone_pending(database, str(ctx.override_id), pending_id)
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE source_overrides SET status='APPROVED',"
                "approved_at='2026-07-22T00:00:00Z',approved_by='analyst',"
                'field_replacements_json=\'[{"code":"000001.SZ",'
                '"trade_date":"2026-07-15","field":"open"}]\' '
                "WHERE override_id=?",
                (pending_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE source_overrides SET status='APPLIED',"
                "applied_at='2026-07-22T00:00:00Z',"
                "applied_version_id='data_version:55555555-5555-4555-8555-555555555555',"
                'field_replacements_json=\'[{"code":"000001.SZ",'
                '"trade_date":"2026-07-15","field":"open"}]\' '
                "WHERE override_id=?",
                (str(ctx.override_id),),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO source_overrides (override_id,dataset,difference_report_id,"
                "primary_batch_id,fallback_batch_id,primary_source,fallback_source,status,"
                "scope_codes_json,scope_dates_json,fields_json,field_replacements_json,"
                "fallback_only_keys_json,exact_scope_sha256,contribution_scope_sha256,reason,"
                "created_at) SELECT 'data_override:66666666-6666-4666-8666-666666666666',"
                "dataset,difference_report_id,primary_batch_id,fallback_batch_id,primary_source,"
                "fallback_source,'PENDING',scope_codes_json,scope_dates_json,fields_json,'[]','[]',"
                "exact_scope_sha256,contribution_scope_sha256,reason,created_at "
                "FROM source_overrides WHERE override_id=?",
                (str(ctx.override_id),),
            )
    ctx.engine.dispose()


def test_existing_0008_exact_scope_upgrades_deterministically(tmp_path: Path) -> None:
    case = tmp_path / "existing_0008"
    case.mkdir()
    ctx = _r5_context(case)
    database = case / "data.db"
    override_id = str(ctx.override_id)
    with ctx.uow_factory() as uow:
        expected_exact_sha = uow.data.get_override(override_id).exact_scope_sha256
        uow.rollback()
    ctx.engine.dispose()
    _run_migration(database, "downgrade", R5_HEAD)
    _run_migration(database, "upgrade", R6_HEAD)
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT exact_scope_sha256,contribution_scope_sha256 "
            "FROM source_overrides WHERE override_id=?",
            (override_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == expected_exact_sha
    assert len(str(row[1])) == 64


@pytest.mark.parametrize("legacy_status", ("PENDING", "APPROVED"))
def test_old_0007_nonterminal_override_blocks_safely(tmp_path: Path, legacy_status: str) -> None:
    case = tmp_path / legacy_status.lower()
    case.mkdir()
    ctx = _r5_context(case)
    database = case / "data.db"
    if legacy_status == "PENDING":
        _clone_pending(
            database,
            str(ctx.override_id),
            "data_override:77777777-7777-4777-8777-777777777777",
        )
    ctx.engine.dispose()
    _run_migration(database, "downgrade", PRE_R5_HEAD)
    before = database.read_bytes()
    request = MigrationRequest(
        run_id=f"r6-{legacy_status.lower()}",
        database_path=database,
        snapshot_root=tmp_path / f"snapshots-{legacy_status.lower()}",
        isolated_root=tmp_path / f"isolated-{legacy_status.lower()}",
        target_revision="head",
        requested_at=utc_now(),
        code_version="s1-04-r6",
        expected_snapshot_revision=PRE_R5_HEAD,
    )
    with pytest.raises(MigrationFailureError) as exc_info:
        migrate_existing_database(request)
    assert exc_info.value.code is MaintenanceFailureCode.ISOLATED_MIGRATION_FAILED
    assert _revision(database) == PRE_R5_HEAD
    assert database.read_bytes() == before
