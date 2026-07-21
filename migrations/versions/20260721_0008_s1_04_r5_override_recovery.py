"""Harden SourceOverride scope, recovery, and terminal facts.

Revision ID: 20260721_0008
Revises: 20260721_0007
Create Date: 2026-07-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260721_0008"
down_revision: str | None = "20260721_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _invalid_count(query: str) -> int:
    return int(op.get_bind().execute(sa.text(query)).scalar_one())


def _validate_existing_rows() -> None:
    checks = {
        "application override FK": """
            SELECT COUNT(*) FROM source_override_applications a
            LEFT JOIN source_overrides o ON o.override_id = a.override_id
            WHERE o.override_id IS NULL
        """,
        "application child FK": """
            SELECT COUNT(*) FROM source_override_applications a
            LEFT JOIN dataset_versions v ON v.version_id = a.child_version_id
            WHERE v.version_id IS NULL
        """,
        "application status": """
            SELECT COUNT(*) FROM source_override_applications
            WHERE status NOT IN ('CLAIMED','FAILED','PUBLISHED','APPLIED')
        """,
        "attempt override FK": """
            SELECT COUNT(*) FROM source_override_application_attempts a
            LEFT JOIN source_overrides o ON o.override_id = a.override_id
            WHERE o.override_id IS NULL
        """,
        "attempt child FK": """
            SELECT COUNT(*) FROM source_override_application_attempts a
            LEFT JOIN dataset_versions v ON v.version_id = a.child_version_id
            WHERE v.version_id IS NULL
        """,
        "attempt status": """
            SELECT COUNT(*) FROM source_override_application_attempts
            WHERE status NOT IN ('CLAIMED','FAILED','PUBLISHED','APPLIED')
        """,
        "application child lineage": """
            SELECT COUNT(*) FROM source_override_applications a
            WHERE NOT EXISTS (
                SELECT 1
                FROM source_overrides o
                JOIN difference_reports r ON r.report_id = o.difference_report_id
                JOIN dataset_versions v ON v.version_id = a.child_version_id
                WHERE o.override_id = a.override_id
                  AND v.dataset = o.dataset
                  AND v.parent_version_id = r.primary_version_id
                  AND v.created_at >= o.approved_at
                  AND (SELECT COUNT(*) FROM dataset_version_batches b
                       WHERE b.version_id = v.version_id) = 2
                  AND EXISTS (SELECT 1 FROM dataset_version_batches b
                              WHERE b.version_id = v.version_id AND b.ordinal = 0
                                AND b.batch_id = o.primary_batch_id)
                  AND EXISTS (SELECT 1 FROM dataset_version_batches b
                              WHERE b.version_id = v.version_id AND b.ordinal = 1
                                AND b.batch_id = o.fallback_batch_id)
            )
        """,
        "attempt child lineage": """
            SELECT COUNT(*) FROM source_override_application_attempts a
            WHERE NOT EXISTS (
                SELECT 1
                FROM source_overrides o
                JOIN difference_reports r ON r.report_id = o.difference_report_id
                JOIN dataset_versions v ON v.version_id = a.child_version_id
                WHERE o.override_id = a.override_id
                  AND v.dataset = o.dataset
                  AND v.parent_version_id = r.primary_version_id
                  AND v.created_at >= o.approved_at
                  AND (SELECT COUNT(*) FROM dataset_version_batches b
                       WHERE b.version_id = v.version_id) = 2
                  AND EXISTS (SELECT 1 FROM dataset_version_batches b
                              WHERE b.version_id = v.version_id AND b.ordinal = 0
                                AND b.batch_id = o.primary_batch_id)
                  AND EXISTS (SELECT 1 FROM dataset_version_batches b
                              WHERE b.version_id = v.version_id AND b.ordinal = 1
                                AND b.batch_id = o.fallback_batch_id)
            )
        """,
        "application state credentials": """
            SELECT COUNT(*)
            FROM source_override_applications a
            JOIN source_overrides o ON o.override_id = a.override_id
            JOIN difference_reports r ON r.report_id = o.difference_report_id
            JOIN dataset_versions v ON v.version_id = a.child_version_id
            WHERE NOT (
                (a.status = 'CLAIMED' AND o.status = 'APPROVED'
                    AND v.status IN ('BUILDING','VALIDATED','PUBLISHED')
                    AND a.report_sha256 IS NULL
                    AND a.contribution_scope_sha256 IS NULL
                    AND a.failure_message IS NULL)
                OR (a.status = 'FAILED' AND o.status = 'APPROVED'
                    AND v.status = 'FAILED'
                    AND a.report_sha256 IS NULL
                    AND a.contribution_scope_sha256 IS NULL
                    AND a.failure_message IS NOT NULL)
                OR (a.status = 'PUBLISHED' AND o.status = 'APPROVED'
                    AND v.status = 'PUBLISHED'
                    AND a.report_sha256 = r.report_sha256
                    AND length(a.contribution_scope_sha256) = 64
                    AND a.failure_message IS NULL)
                OR (a.status = 'APPLIED' AND o.status = 'APPLIED'
                    AND o.applied_version_id = a.child_version_id
                    AND v.status = 'PUBLISHED'
                    AND a.report_sha256 = r.report_sha256
                    AND length(a.contribution_scope_sha256) = 64
                    AND a.failure_message IS NULL)
            )
        """,
        "attempt state credentials": """
            SELECT COUNT(*)
            FROM source_override_application_attempts a
            JOIN dataset_versions v ON v.version_id = a.child_version_id
            WHERE NOT (
                (a.status = 'CLAIMED' AND a.ended_at IS NULL
                    AND a.failure_message IS NULL
                    AND v.status IN ('BUILDING','VALIDATED','PUBLISHED'))
                OR (a.status = 'FAILED' AND a.ended_at IS NOT NULL
                    AND a.failure_message IS NOT NULL AND v.status = 'FAILED')
                OR (a.status IN ('PUBLISHED','APPLIED')
                    AND a.ended_at IS NOT NULL AND a.failure_message IS NULL
                    AND v.status = 'PUBLISHED')
            )
        """,
    }
    for label, query in checks.items():
        if _invalid_count(query):
            raise RuntimeError(f"cannot upgrade invalid 0007 data: {label}")


def _create_application_tables() -> None:
    op.create_table(
        "source_override_applications",
        sa.Column("application_id", sa.String(length=96), primary_key=True),
        sa.Column(
            "override_id",
            sa.String(length=96),
            sa.ForeignKey("source_overrides.override_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "child_version_id",
            sa.String(length=96),
            sa.ForeignKey("dataset_versions.version_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("report_sha256", sa.String(length=64), nullable=True),
        sa.Column("contribution_scope_sha256", sa.String(length=64), nullable=True),
        sa.Column("claimed_by", sa.String(length=256), nullable=False),
        sa.Column("claimed_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.UniqueConstraint("child_version_id", name="uq_source_override_applications_child"),
        sa.CheckConstraint(
            "status IN ('CLAIMED','FAILED','PUBLISHED','APPLIED')",
            name="ck_source_override_applications_status",
        ),
    )
    op.create_index(
        "uq_source_override_applications_active_override",
        "source_override_applications",
        ["override_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('CLAIMED','PUBLISHED','APPLIED')"),
    )
    op.create_table(
        "source_override_application_attempts",
        sa.Column("attempt_id", sa.String(length=96), primary_key=True),
        sa.Column(
            "override_id",
            sa.String(length=96),
            sa.ForeignKey("source_overrides.override_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column(
            "child_version_id",
            sa.String(length=96),
            sa.ForeignKey("dataset_versions.version_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("claimed_by", sa.String(length=256), nullable=False),
        sa.Column("started_at", sa.String(length=40), nullable=False),
        sa.Column("ended_at", sa.String(length=40), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.UniqueConstraint("override_id", "attempt_no", name="uq_override_attempt_no"),
        sa.CheckConstraint(
            "status IN ('CLAIMED','FAILED','PUBLISHED','APPLIED')",
            name="ck_source_override_application_attempts_status",
        ),
    )
    op.create_index(
        "ix_override_attempts_override_status",
        "source_override_application_attempts",
        ["override_id", "status"],
    )


def _rebuild_from_0007() -> None:
    op.drop_index(
        "ix_override_attempts_override_status",
        table_name="source_override_application_attempts",
    )
    op.execute(
        "ALTER TABLE source_override_applications RENAME TO source_override_applications_0007"
    )
    op.execute(
        "ALTER TABLE source_override_application_attempts "
        "RENAME TO source_override_application_attempts_0007"
    )
    _create_application_tables()
    columns = (
        "application_id,override_id,child_version_id,input_fingerprint,status,"
        "report_sha256,contribution_scope_sha256,claimed_by,claimed_at,updated_at,"
        "failure_message"
    )
    op.execute(
        f"INSERT INTO source_override_applications ({columns}) "
        f"SELECT {columns} FROM source_override_applications_0007"
    )
    attempt_columns = (
        "attempt_id,override_id,attempt_no,child_version_id,input_fingerprint,status,"
        "claimed_by,started_at,ended_at,failure_message"
    )
    op.execute(
        f"INSERT INTO source_override_application_attempts ({attempt_columns}) "
        f"SELECT {attempt_columns} FROM source_override_application_attempts_0007"
    )
    op.drop_table("source_override_application_attempts_0007")
    op.drop_table("source_override_applications_0007")


def _lineage_checks() -> str:
    return """
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM source_overrides o
            WHERE o.override_id = NEW.override_id AND o.status = 'APPROVED'
        ) THEN RAISE(ABORT, 'override application requires APPROVED override') END;
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1
            FROM source_overrides o
            JOIN difference_reports r ON r.report_id = o.difference_report_id
            JOIN dataset_versions v ON v.version_id = NEW.child_version_id
            WHERE o.override_id = NEW.override_id
              AND v.dataset = o.dataset
              AND v.parent_version_id = r.primary_version_id
              AND v.created_at >= o.approved_at
              AND (SELECT COUNT(*) FROM dataset_version_batches b
                   WHERE b.version_id = v.version_id) = 2
              AND EXISTS (SELECT 1 FROM dataset_version_batches b
                          WHERE b.version_id = v.version_id AND b.ordinal = 0
                            AND b.batch_id = o.primary_batch_id)
              AND EXISTS (SELECT 1 FROM dataset_version_batches b
                          WHERE b.version_id = v.version_id AND b.ordinal = 1
                            AND b.batch_id = o.fallback_batch_id)
        ) THEN RAISE(ABORT, 'override application child lineage is invalid') END;
    """


def _create_triggers() -> None:
    lineage = _lineage_checks()
    op.execute(
        f"""
        CREATE TRIGGER source_override_applications_validate_insert
        BEFORE INSERT ON source_override_applications
        BEGIN
            {lineage}
            SELECT CASE WHEN NEW.status != 'CLAIMED'
                THEN RAISE(ABORT, 'new override application must be CLAIMED') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_override_applications_validate_update
        BEFORE UPDATE ON source_override_applications
        WHEN OLD.status = 'CLAIMED'
        BEGIN
            SELECT CASE WHEN NEW.override_id != OLD.override_id
                              OR NEW.child_version_id != OLD.child_version_id
                              OR NEW.input_fingerprint != OLD.input_fingerprint
                              OR NEW.claimed_by != OLD.claimed_by
                              OR NEW.claimed_at != OLD.claimed_at
                THEN RAISE(ABORT, 'override application identity is immutable') END;
            SELECT CASE WHEN NEW.status NOT IN ('FAILED','PUBLISHED')
                THEN RAISE(ABORT, 'invalid override application transition') END;
            SELECT CASE WHEN NEW.status = 'PUBLISHED' AND NOT EXISTS (
                SELECT 1 FROM dataset_versions v
                JOIN source_overrides o ON o.override_id = NEW.override_id
                JOIN difference_reports r ON r.report_id = o.difference_report_id
                WHERE v.version_id = NEW.child_version_id
                  AND v.status = 'PUBLISHED'
                  AND NEW.report_sha256 = r.report_sha256
                  AND length(NEW.contribution_scope_sha256) = 64
                  AND NEW.failure_message IS NULL
            ) THEN RAISE(ABORT, 'published override application credentials are invalid') END;
            SELECT CASE WHEN NEW.status = 'FAILED'
                              AND (NEW.report_sha256 IS NOT NULL
                                   OR NEW.contribution_scope_sha256 IS NOT NULL
                                   OR NEW.failure_message IS NULL
                                   OR NOT EXISTS (
                                       SELECT 1 FROM dataset_versions v
                                       WHERE v.version_id = NEW.child_version_id
                                         AND v.status = 'FAILED'
                                   ))
                THEN RAISE(ABORT, 'failed override application facts are invalid') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_override_applications_freeze_terminal_update
        BEFORE UPDATE ON source_override_applications
        WHEN OLD.status IN ('PUBLISHED','APPLIED','FAILED')
        BEGIN
            SELECT CASE WHEN OLD.status != 'PUBLISHED' OR NEW.status != 'APPLIED'
                              OR NEW.application_id != OLD.application_id
                              OR NEW.override_id != OLD.override_id
                              OR NEW.child_version_id != OLD.child_version_id
                              OR NEW.input_fingerprint != OLD.input_fingerprint
                              OR NEW.report_sha256 != OLD.report_sha256
                              OR NEW.contribution_scope_sha256 != OLD.contribution_scope_sha256
                              OR NEW.claimed_by != OLD.claimed_by
                              OR NEW.claimed_at != OLD.claimed_at
                              OR NEW.failure_message IS NOT OLD.failure_message
                THEN RAISE(ABORT, 'terminal override application facts are immutable') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM source_overrides o
                WHERE o.override_id = OLD.override_id
                  AND o.status = 'APPLIED'
                  AND o.applied_version_id = OLD.child_version_id
            ) THEN RAISE(ABORT, 'APPLIED application requires APPLIED override') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_override_applications_freeze_terminal_delete
        BEFORE DELETE ON source_override_applications
        WHEN OLD.status IN ('PUBLISHED','APPLIED','FAILED')
        BEGIN
            SELECT RAISE(ABORT, 'terminal override application cannot be deleted');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER source_override_attempts_validate_insert
        BEFORE INSERT ON source_override_application_attempts
        BEGIN
            {lineage}
            SELECT CASE WHEN NEW.status != 'CLAIMED'
                THEN RAISE(ABORT, 'new override application attempt must be CLAIMED') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_override_attempts_validate_update
        BEFORE UPDATE ON source_override_application_attempts
        WHEN OLD.status = 'CLAIMED'
        BEGIN
            SELECT CASE WHEN NEW.override_id != OLD.override_id
                              OR NEW.child_version_id != OLD.child_version_id
                              OR NEW.input_fingerprint != OLD.input_fingerprint
                              OR NEW.claimed_by != OLD.claimed_by
                              OR NEW.started_at != OLD.started_at
                THEN RAISE(ABORT, 'override attempt identity is immutable') END;
            SELECT CASE WHEN NEW.status NOT IN ('FAILED','PUBLISHED')
                THEN RAISE(ABORT, 'invalid override attempt transition') END;
            SELECT CASE WHEN NEW.status IN ('PUBLISHED') AND NOT EXISTS (
                SELECT 1 FROM dataset_versions v
                WHERE v.version_id = NEW.child_version_id AND v.status = 'PUBLISHED'
            ) THEN RAISE(ABORT, 'override attempt requires PUBLISHED child') END;
            SELECT CASE WHEN NEW.status = 'FAILED' AND NOT EXISTS (
                SELECT 1 FROM dataset_versions v
                WHERE v.version_id = NEW.child_version_id AND v.status = 'FAILED'
            ) THEN RAISE(ABORT, 'failed override attempt requires FAILED child') END;
            SELECT CASE WHEN NEW.ended_at IS NULL
                THEN RAISE(ABORT, 'terminal override attempt requires ended_at') END;
            SELECT CASE WHEN NEW.status = 'FAILED' AND NEW.failure_message IS NULL
                THEN RAISE(ABORT, 'failed override attempt requires failure evidence') END;
            SELECT CASE WHEN NEW.status = 'PUBLISHED' AND NEW.failure_message IS NOT NULL
                THEN RAISE(ABORT, 'published override attempt cannot contain failure evidence') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_override_attempts_freeze_terminal_update
        BEFORE UPDATE ON source_override_application_attempts
        WHEN OLD.status IN ('PUBLISHED','APPLIED','FAILED')
        BEGIN
            SELECT CASE WHEN OLD.status != 'PUBLISHED' OR NEW.status != 'APPLIED'
                              OR NEW.attempt_id != OLD.attempt_id
                              OR NEW.override_id != OLD.override_id
                              OR NEW.attempt_no != OLD.attempt_no
                              OR NEW.child_version_id != OLD.child_version_id
                              OR NEW.input_fingerprint != OLD.input_fingerprint
                              OR NEW.claimed_by != OLD.claimed_by
                              OR NEW.started_at != OLD.started_at
                              OR NEW.ended_at != OLD.ended_at
                              OR NEW.failure_message IS NOT OLD.failure_message
                THEN RAISE(ABORT, 'terminal override attempt facts are immutable') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM source_override_applications a
                WHERE a.override_id = OLD.override_id
                  AND a.child_version_id = OLD.child_version_id
                  AND a.status = 'APPLIED'
            ) THEN RAISE(ABORT, 'APPLIED attempt requires APPLIED application') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_override_attempts_freeze_terminal_delete
        BEFORE DELETE ON source_override_application_attempts
        WHEN OLD.status IN ('PUBLISHED','APPLIED','FAILED')
        BEGIN
            SELECT RAISE(ABORT, 'terminal override attempt cannot be deleted');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_overrides_require_published_application
        BEFORE UPDATE ON source_overrides
        WHEN NEW.status = 'APPLIED' AND OLD.status = 'APPROVED'
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM source_override_applications a
                WHERE a.override_id = OLD.override_id
                  AND a.child_version_id = NEW.applied_version_id
                  AND a.status = 'PUBLISHED'
                  AND length(a.report_sha256) = 64
                  AND length(a.contribution_scope_sha256) = 64
            ) THEN RAISE(ABORT, 'APPLIED override requires PUBLISHED application') END;
        END
        """
    )


def _drop_triggers() -> None:
    for name in (
        "source_overrides_require_published_application",
        "source_override_attempts_freeze_terminal_delete",
        "source_override_attempts_freeze_terminal_update",
        "source_override_attempts_validate_update",
        "source_override_attempts_validate_insert",
        "source_override_applications_freeze_terminal_delete",
        "source_override_applications_freeze_terminal_update",
        "source_override_applications_validate_update",
        "source_override_applications_validate_insert",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def _restore_0007_source_override_triggers() -> None:
    """Restore the complete 0007 trigger set after SQLite batch recreation."""
    op.execute(
        "CREATE TRIGGER source_overrides_validate_insert BEFORE INSERT ON source_overrides "
        "WHEN NEW.status <> 'PENDING' OR NEW.primary_batch_id=NEW.fallback_batch_id OR NOT EXISTS "
        "(SELECT 1 FROM ingestion_batches p, ingestion_batches f, difference_reports d, "
        "dataset_versions v WHERE p.batch_id=NEW.primary_batch_id "
        "AND p.source=NEW.primary_source AND p.dataset=NEW.dataset "
        "AND f.batch_id=NEW.fallback_batch_id AND f.source=NEW.fallback_source "
        "AND f.dataset=NEW.dataset AND d.report_id=NEW.difference_report_id "
        "AND d.primary_source=NEW.primary_source AND d.fallback_source=NEW.fallback_source "
        "AND d.primary_batch_id=NEW.primary_batch_id "
        "AND d.fallback_batch_id=NEW.fallback_batch_id "
        "AND v.version_id=d.version_id AND v.dataset=NEW.dataset) "
        "BEGIN SELECT RAISE(ABORT, 'source override lineage is inconsistent'); END"
    )
    immutable = (
        "NEW.override_id IS NOT OLD.override_id OR NEW.dataset IS NOT OLD.dataset "
        "OR NEW.difference_report_id IS NOT OLD.difference_report_id "
        "OR NEW.primary_batch_id IS NOT OLD.primary_batch_id "
        "OR NEW.fallback_batch_id IS NOT OLD.fallback_batch_id "
        "OR NEW.primary_source IS NOT OLD.primary_source "
        "OR NEW.fallback_source IS NOT OLD.fallback_source "
        "OR NEW.scope_codes_json IS NOT OLD.scope_codes_json "
        "OR NEW.scope_dates_json IS NOT OLD.scope_dates_json "
        "OR NEW.fields_json IS NOT OLD.fields_json OR NEW.reason IS NOT OLD.reason "
        "OR NEW.created_at IS NOT OLD.created_at "
    )
    op.execute(
        "CREATE TRIGGER source_overrides_pending_transition BEFORE UPDATE ON source_overrides "
        "WHEN OLD.status='PENDING' AND (NEW.status NOT IN ('APPROVED','REJECTED') OR "
        f"{immutable}"
        "OR NEW.approved_at IS NULL OR NEW.approved_by IS NULL "
        "OR NEW.applied_at IS NOT NULL OR NEW.applied_version_id IS NOT NULL) "
        "BEGIN SELECT RAISE(ABORT, 'invalid source override decision transition'); END"
    )
    op.execute(
        "CREATE TRIGGER source_overrides_approved_transition BEFORE UPDATE ON source_overrides "
        "WHEN OLD.status='APPROVED' AND (NEW.status <> 'APPLIED' OR "
        f"{immutable}"
        "OR NEW.approved_at IS NOT OLD.approved_at OR NEW.approved_by IS NOT OLD.approved_by "
        "OR NEW.applied_at IS NULL OR NEW.applied_version_id IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM dataset_versions v JOIN difference_reports d "
        "ON d.report_id=OLD.difference_report_id WHERE v.version_id=NEW.applied_version_id "
        "AND v.dataset=OLD.dataset AND v.status='PUBLISHED' AND v.version_id<>d.version_id "
        "AND v.parent_version_id=d.primary_version_id AND v.created_at>=OLD.approved_at)) "
        "BEGIN SELECT RAISE(ABORT, 'approved override requires a published direct child'); END"
    )
    op.execute(
        "CREATE TRIGGER source_overrides_no_update_terminal BEFORE UPDATE ON source_overrides "
        "WHEN OLD.status IN ('REJECTED','APPLIED') "
        "BEGIN SELECT RAISE(ABORT, 'terminal source overrides are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER source_overrides_no_delete_decided BEFORE DELETE ON source_overrides "
        "WHEN OLD.status IN ('APPROVED','REJECTED','APPLIED') "
        "BEGIN SELECT RAISE(ABORT, 'decided source overrides are immutable'); END"
    )


def upgrade() -> None:
    _validate_existing_rows()
    op.add_column(
        "source_overrides",
        sa.Column(
            "field_replacements_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "source_overrides",
        sa.Column(
            "fallback_only_keys_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    _rebuild_from_0007()
    _create_triggers()


def downgrade() -> None:
    _drop_triggers()
    op.drop_index(
        "uq_source_override_applications_active_override",
        table_name="source_override_applications",
    )
    op.drop_index(
        "ix_override_attempts_override_status",
        table_name="source_override_application_attempts",
    )
    op.execute(
        "ALTER TABLE source_override_applications RENAME TO source_override_applications_0008"
    )
    op.execute(
        "ALTER TABLE source_override_application_attempts "
        "RENAME TO source_override_application_attempts_0008"
    )
    op.create_table(
        "source_override_applications",
        sa.Column("application_id", sa.String(length=96), primary_key=True),
        sa.Column("override_id", sa.String(length=96), nullable=False),
        sa.Column("child_version_id", sa.String(length=96), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("report_sha256", sa.String(length=64), nullable=True),
        sa.Column("contribution_scope_sha256", sa.String(length=64), nullable=True),
        sa.Column("claimed_by", sa.String(length=256), nullable=False),
        sa.Column("claimed_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.UniqueConstraint("override_id", name="uq_source_override_applications_override"),
        sa.UniqueConstraint("child_version_id", name="uq_source_override_applications_child"),
        sa.CheckConstraint(
            "status IN ('CLAIMED','FAILED','PUBLISHED','APPLIED')",
            name="ck_source_override_applications_status",
        ),
    )
    op.create_table(
        "source_override_application_attempts",
        sa.Column("attempt_id", sa.String(length=96), primary_key=True),
        sa.Column("override_id", sa.String(length=96), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("child_version_id", sa.String(length=96), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("claimed_by", sa.String(length=256), nullable=False),
        sa.Column("started_at", sa.String(length=40), nullable=False),
        sa.Column("ended_at", sa.String(length=40), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.UniqueConstraint("override_id", "attempt_no", name="uq_override_attempt_no"),
    )
    app_columns = (
        "application_id,override_id,child_version_id,input_fingerprint,status,"
        "report_sha256,contribution_scope_sha256,claimed_by,claimed_at,updated_at,"
        "failure_message"
    )
    op.execute(
        f"INSERT INTO source_override_applications ({app_columns}) "
        f"SELECT {app_columns} FROM source_override_applications_0008 "
        "WHERE status != 'FAILED'"
    )
    attempt_columns = (
        "attempt_id,override_id,attempt_no,child_version_id,input_fingerprint,status,"
        "claimed_by,started_at,ended_at,failure_message"
    )
    op.execute(
        f"INSERT INTO source_override_application_attempts ({attempt_columns}) "
        f"SELECT {attempt_columns} FROM source_override_application_attempts_0008"
    )
    op.drop_table("source_override_application_attempts_0008")
    op.drop_table("source_override_applications_0008")
    op.create_index(
        "ix_override_attempts_override_status",
        "source_override_application_attempts",
        ["override_id", "status"],
    )
    with op.batch_alter_table("source_overrides", recreate="always") as batch:
        batch.drop_column("fallback_only_keys_json")
        batch.drop_column("field_replacements_json")
    _restore_0007_source_override_triggers()
