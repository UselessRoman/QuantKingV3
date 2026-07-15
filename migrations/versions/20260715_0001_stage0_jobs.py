"""Create Stage 0 durable jobs, events, dependencies, and append-only audit."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260715_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("job_id", sa.String(length=64), primary_key=True),
        sa.Column("job_type", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("input_manifest_json", sa.Text(), nullable=False),
        sa.Column("config_manifest_json", sa.Text(), nullable=False),
        sa.Column("version_manifest_json", sa.Text(), nullable=False),
        sa.Column("requester", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("started_at", sa.String(length=40)),
        sa.Column("ended_at", sa.String(length=40)),
        sa.Column("heartbeat_at", sa.String(length=40)),
        sa.Column("stage", sa.String(length=256)),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("worker_id", sa.String(length=256)),
        sa.Column("log_id", sa.String(length=512), nullable=False),
        sa.Column("artifacts_json", sa.Text(), nullable=False),
        sa.Column("failure_json", sa.Text()),
        sa.Column("cancel_requested_at", sa.String(length=40)),
        sa.Column("cancel_request", sa.Text()),
        sa.Column("cancel_result", sa.Text()),
        sa.Column("non_cancellable_phase", sa.String(length=256)),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED','INTERRUPTED')",
            name="ck_job_runs_status",
        ),
        sa.CheckConstraint("progress >= 0 AND progress <= 1", name="ck_job_runs_progress"),
        sa.CheckConstraint("row_version >= 1", name="ck_job_runs_row_version"),
        sa.UniqueConstraint("job_type", "idempotency_key", name="uq_job_runs_type_idempotency"),
    )
    op.create_table(
        "job_dependencies",
        sa.Column(
            "job_id",
            sa.String(length=64),
            sa.ForeignKey("job_runs.job_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "upstream_job_id",
            sa.String(length=64),
            sa.ForeignKey("job_runs.job_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
    )
    op.create_table(
        "job_events",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            sa.String(length=64),
            sa.ForeignKey("job_runs.job_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("stage", sa.String(length=256)),
        sa.Column("progress", sa.Float()),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.String(length=40), nullable=False),
        sa.CheckConstraint(
            "progress IS NULL OR (progress >= 0 AND progress <= 1)",
            name="ck_events_progress",
        ),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_job_events_job_event", "job_events", ["job_id", "event_id"])
    op.create_table(
        "audit_records",
        sa.Column("audit_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor", sa.String(length=256), nullable=False),
        sa.Column("entity", sa.String(length=256), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("correlation_id", sa.String(length=256), nullable=False),
        sa.Column("occurred_at", sa.String(length=40), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_audit_records_action_id", "audit_records", ["action", "audit_id"])
    op.execute(
        "CREATE TRIGGER audit_records_no_update BEFORE UPDATE ON audit_records "
        "BEGIN SELECT RAISE(ABORT, 'audit_records are append-only'); END"
    )
    op.execute(
        "CREATE TRIGGER audit_records_no_delete BEFORE DELETE ON audit_records "
        "BEGIN SELECT RAISE(ABORT, 'audit_records are append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_records_no_delete")
    op.execute("DROP TRIGGER IF EXISTS audit_records_no_update")
    op.drop_index("ix_audit_records_action_id", table_name="audit_records")
    op.drop_table("audit_records")
    op.drop_index("ix_job_events_job_event", table_name="job_events")
    op.drop_table("job_events")
    op.drop_table("job_dependencies")
    op.drop_table("job_runs")
