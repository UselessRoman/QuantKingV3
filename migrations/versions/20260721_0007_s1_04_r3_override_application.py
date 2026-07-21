"""Add recoverable SourceOverride application bindings.

Revision ID: 20260721_0007
Revises: 20260720_0006
Create Date: 2026-07-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260721_0007"
down_revision: str | None = "20260720_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
    _create_validation_triggers()


def _create_validation_triggers() -> None:
    lineage_checks = """
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
    op.execute(
        f"""
        CREATE TRIGGER source_override_applications_validate_insert
        BEFORE INSERT ON source_override_applications
        BEGIN
            {lineage_checks}
            SELECT CASE WHEN NEW.status != 'CLAIMED'
                THEN RAISE(ABORT, 'new override application must be CLAIMED') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_override_applications_validate_update
        BEFORE UPDATE ON source_override_applications
        BEGIN
            SELECT CASE WHEN NEW.override_id != OLD.override_id
                              OR NEW.child_version_id != OLD.child_version_id
                              OR NEW.input_fingerprint != OLD.input_fingerprint
                THEN RAISE(ABORT, 'override application identity is immutable') END;
            SELECT CASE WHEN NOT (
                (OLD.status = 'CLAIMED' AND NEW.status IN ('CLAIMED','FAILED','PUBLISHED')) OR
                (OLD.status = 'PUBLISHED' AND NEW.status IN ('PUBLISHED','APPLIED')) OR
                (OLD.status = NEW.status)
            ) THEN RAISE(ABORT, 'invalid override application transition') END;
            SELECT CASE WHEN NEW.status = 'PUBLISHED' AND NOT EXISTS (
                SELECT 1 FROM dataset_versions v
                JOIN source_overrides o ON o.override_id = NEW.override_id
                JOIN difference_reports r ON r.report_id = o.difference_report_id
                WHERE v.version_id = NEW.child_version_id
                  AND v.status = 'PUBLISHED'
                  AND NEW.report_sha256 = r.report_sha256
                  AND length(NEW.contribution_scope_sha256) = 64
            ) THEN RAISE(ABORT, 'published override application credentials are invalid') END;
            SELECT CASE WHEN NEW.status = 'APPLIED' AND NOT EXISTS (
                SELECT 1 FROM source_overrides o
                WHERE o.override_id = NEW.override_id AND o.status = 'APPLIED'
                  AND o.applied_version_id = NEW.child_version_id
            ) THEN RAISE(ABORT, 'applied override application binding is invalid') END;
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER source_override_attempts_validate_insert
        BEFORE INSERT ON source_override_application_attempts
        BEGIN
            {lineage_checks}
            SELECT CASE WHEN NEW.status != 'CLAIMED'
                THEN RAISE(ABORT, 'new override application attempt must be CLAIMED') END;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_override_attempts_validate_update
        BEFORE UPDATE ON source_override_application_attempts
        BEGIN
            SELECT CASE WHEN NEW.override_id != OLD.override_id
                              OR NEW.child_version_id != OLD.child_version_id
                              OR NEW.input_fingerprint != OLD.input_fingerprint
                THEN RAISE(ABORT, 'override application attempt identity is immutable') END;
            SELECT CASE WHEN NOT (
                (OLD.status = 'CLAIMED' AND NEW.status IN ('CLAIMED','FAILED','PUBLISHED')) OR
                (OLD.status = 'PUBLISHED' AND NEW.status IN ('PUBLISHED','APPLIED')) OR
                (OLD.status = NEW.status)
            ) THEN RAISE(ABORT, 'invalid override application attempt transition') END;
            SELECT CASE WHEN NEW.status IN ('PUBLISHED','APPLIED') AND NOT EXISTS (
                SELECT 1 FROM dataset_versions v
                WHERE v.version_id = NEW.child_version_id AND v.status = 'PUBLISHED'
            ) THEN RAISE(ABORT, 'override attempt requires PUBLISHED child') END;
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


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS source_overrides_require_published_application")
    op.execute("DROP TRIGGER IF EXISTS source_override_attempts_validate_update")
    op.execute("DROP TRIGGER IF EXISTS source_override_attempts_validate_insert")
    op.execute("DROP TRIGGER IF EXISTS source_override_applications_validate_update")
    op.execute("DROP TRIGGER IF EXISTS source_override_applications_validate_insert")
    op.drop_index(
        "ix_override_attempts_override_status", table_name="source_override_application_attempts"
    )
    op.drop_table("source_override_application_attempts")
    op.drop_table("source_override_applications")
