"""Add immutable K2 resolution run summaries without touching historical override bytes.

Revision ID: 20260810_0014
Revises: 20260808_0013
Create Date: 2026-08-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260810_0014"
down_revision: str | None = "20260808_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resolution_runs",
        sa.Column("run_id", sa.String(length=96), primary_key=True),
        sa.Column("dataset", sa.String(length=64), nullable=False),
        sa.Column(
            "created_job_id",
            sa.String(length=64),
            sa.ForeignKey("job_runs.job_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "candidate_version_id",
            sa.String(length=96),
            sa.ForeignKey("dataset_versions.version_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("rule_version", sa.String(length=128), nullable=False),
        sa.Column("threshold_rule_version", sa.String(length=128), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("artifact_relative_path", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("total_decisions", sa.Integer(), nullable=False),
        sa.Column("default_decision_count", sa.Integer(), nullable=False),
        sa.Column("exception_count", sa.Integer(), nullable=False),
        sa.Column("conflict_count", sa.Integer(), nullable=False),
        sa.Column("blocker_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "dataset = 'daily_bar_unadjusted'",
            name="ck_resolution_runs_daily_only",
        ),
        sa.CheckConstraint(
            "length(input_fingerprint) = 64 AND length(manifest_sha256) = 64",
            name="ck_resolution_runs_sha_lengths",
        ),
        sa.CheckConstraint(
            "total_decisions >= 0 AND default_decision_count >= 0 "
            "AND exception_count >= 0 AND conflict_count >= 0 AND blocker_count >= 0 "
            "AND total_decisions = default_decision_count + exception_count + conflict_count",
            name="ck_resolution_runs_counts",
        ),
        sa.CheckConstraint(
            "(conflict_count = 0 AND blocker_count = 0 "
            "AND candidate_version_id IS NOT NULL) "
            "OR ((conflict_count > 0 OR blocker_count > 0) "
            "AND candidate_version_id IS NULL)",
            name="ck_resolution_runs_candidate",
        ),
        sa.UniqueConstraint("created_job_id", name="uq_resolution_runs_job"),
    )
    op.create_index(
        "ix_resolution_runs_created_at",
        "resolution_runs",
        ["created_at", "run_id"],
    )
    op.create_index(
        "ix_resolution_runs_fingerprint",
        "resolution_runs",
        ["input_fingerprint"],
    )
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER resolution_runs_immutable_{action.lower()}
            BEFORE {action} ON resolution_runs
            BEGIN
              SELECT RAISE(ABORT, 'resolution run rows are immutable');
            END
            """
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS resolution_runs_immutable_delete")
    op.execute("DROP TRIGGER IF EXISTS resolution_runs_immutable_update")
    op.drop_index("ix_resolution_runs_fingerprint", table_name="resolution_runs")
    op.drop_index("ix_resolution_runs_created_at", table_name="resolution_runs")
    op.drop_table("resolution_runs")
