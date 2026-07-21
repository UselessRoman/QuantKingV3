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
    op.create_index(
        "ix_override_attempts_override_status",
        "source_override_application_attempts",
        ["override_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_override_attempts_override_status", table_name="source_override_application_attempts"
    )
    op.drop_table("source_override_application_attempts")
    op.drop_table("source_override_applications")
