"""Create Stage 1 data metadata tables with immutable-state triggers.

Revision ID: 20260716_0001
Revises: 20260715_0001
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260716_0001"
down_revision: str | None = "20260715_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_batches",
        sa.Column("batch_id", sa.String(length=96), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("dataset", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("adapter_version", sa.String(length=128), nullable=False),
        sa.Column("request_params_json", sa.Text(), nullable=False),
        sa.Column("request_params_sha256", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.String(length=40), nullable=False),
        sa.Column("ended_at", sa.String(length=40)),
        sa.Column("row_count", sa.Integer()),
        sa.Column("security_count", sa.Integer()),
        sa.Column("manifest_files_json", sa.Text(), nullable=False),
        sa.Column("failure_json", sa.Text()),
        sa.Column(
            "job_run_id",
            sa.String(length=64),
            sa.ForeignKey("job_runs.job_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','PARTIAL','FAILED')",
            name="ck_ingestion_batches_status",
        ),
        sa.CheckConstraint(
            "row_count IS NULL OR row_count >= 0", name="ck_ingestion_batches_row_count"
        ),
        sa.CheckConstraint(
            "security_count IS NULL OR security_count >= 0",
            name="ck_ingestion_batches_security_count",
        ),
        sa.UniqueConstraint(
            "source",
            "dataset",
            "request_params_sha256",
            name="uq_batches_source_dataset_params",
        ),
    )
    op.create_index(
        "ix_ingestion_batches_dataset_status", "ingestion_batches", ["dataset", "status"]
    )

    op.create_table(
        "dataset_versions",
        sa.Column("version_id", sa.String(length=96), primary_key=True),
        sa.Column("dataset", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.String(length=128), nullable=False),
        sa.Column("code_version", sa.String(length=128), nullable=False),
        sa.Column("rule_version", sa.String(length=128), nullable=False),
        sa.Column("parent_version_id", sa.String(length=96)),
        sa.Column("input_batch_ids_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("validated_at", sa.String(length=40)),
        sa.Column("published_at", sa.String(length=40)),
        sa.Column("manifest_sha256", sa.String(length=64)),
        sa.Column("security_count", sa.Integer()),
        sa.Column("row_count", sa.Integer()),
        sa.Column("failure_json", sa.Text()),
        sa.CheckConstraint(
            "status IN ('BUILDING','VALIDATED','PUBLISHED','FAILED')",
            name="ck_dataset_versions_status",
        ),
        sa.CheckConstraint(
            "security_count IS NULL OR security_count >= 0",
            name="ck_dataset_versions_security_count",
        ),
        sa.CheckConstraint(
            "row_count IS NULL OR row_count >= 0", name="ck_dataset_versions_row_count"
        ),
    )
    op.create_index("ix_dataset_versions_dataset_status", "dataset_versions", ["dataset", "status"])

    op.create_table(
        "quality_runs",
        sa.Column("run_id", sa.String(length=96), primary_key=True),
        sa.Column(
            "version_id",
            sa.String(length=96),
            sa.ForeignKey("dataset_versions.version_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("rule_version", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.String(length=40), nullable=False),
        sa.Column("ended_at", sa.String(length=40)),
        sa.Column("results_json", sa.Text(), nullable=False),
        sa.Column("summary_report_relative_path", sa.Text()),
        sa.Column("failure_json", sa.Text()),
        sa.CheckConstraint(
            "status IN ('RUNNING','PASSED','FAILED','SKIPPED')",
            name="ck_quality_runs_status",
        ),
    )
    op.create_index("ix_quality_runs_version", "quality_runs", ["version_id"])

    op.create_table(
        "difference_reports",
        sa.Column("report_id", sa.String(length=96), primary_key=True),
        sa.Column(
            "version_id",
            sa.String(length=96),
            sa.ForeignKey("dataset_versions.version_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("primary_source", sa.String(length=32), nullable=False),
        sa.Column("fallback_source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("field_differences_json", sa.Text(), nullable=False),
        sa.Column("total_compared", sa.Integer(), nullable=False),
        sa.Column("total_differences", sa.Integer(), nullable=False),
        sa.Column("report_relative_path", sa.Text(), nullable=False),
    )
    op.create_index("ix_difference_reports_version", "difference_reports", ["version_id"])

    op.create_table(
        "source_overrides",
        sa.Column("override_id", sa.String(length=96), primary_key=True),
        sa.Column("dataset", sa.String(length=64), nullable=False),
        sa.Column("primary_source", sa.String(length=32), nullable=False),
        sa.Column("fallback_source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("scope_codes_json", sa.Text(), nullable=False),
        sa.Column("scope_dates_json", sa.Text(), nullable=False),
        sa.Column("fields_json", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("approved_at", sa.String(length=40)),
        sa.Column("approved_by", sa.String(length=256)),
        sa.Column("applied_at", sa.String(length=40)),
        sa.Column("applied_version_id", sa.String(length=96)),
        sa.CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','APPLIED')",
            name="ck_source_overrides_status",
        ),
    )
    op.create_index("ix_source_overrides_dataset_status", "source_overrides", ["dataset", "status"])

    # Immutable-state triggers: PUBLISHED dataset versions and APPLIED overrides
    # cannot be UPDATEd or DELETEd in place. Retries must create new IDs. An
    # APPROVED override may still transition to APPLIED (a controlled state
    # change), but once APPLIED it is fully immutable.
    op.execute(
        "CREATE TRIGGER dataset_versions_no_update_published BEFORE UPDATE ON dataset_versions "
        "WHEN OLD.status = 'PUBLISHED' "
        "BEGIN SELECT RAISE(ABORT, 'published dataset versions are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER dataset_versions_no_delete_published BEFORE DELETE ON dataset_versions "
        "WHEN OLD.status = 'PUBLISHED' "
        "BEGIN SELECT RAISE(ABORT, 'published dataset versions are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER source_overrides_no_update_applied BEFORE UPDATE ON source_overrides "
        "WHEN OLD.status = 'APPLIED' "
        "BEGIN SELECT RAISE(ABORT, 'applied source overrides are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER source_overrides_no_delete_applied BEFORE DELETE ON source_overrides "
        "WHEN OLD.status = 'APPLIED' "
        "BEGIN SELECT RAISE(ABORT, 'applied source overrides are immutable'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS source_overrides_no_delete_applied")
    op.execute("DROP TRIGGER IF EXISTS source_overrides_no_update_applied")
    op.execute("DROP TRIGGER IF EXISTS dataset_versions_no_delete_published")
    op.execute("DROP TRIGGER IF EXISTS dataset_versions_no_update_published")
    op.drop_index("ix_source_overrides_dataset_status", table_name="source_overrides")
    op.drop_table("source_overrides")
    op.drop_index("ix_difference_reports_version", table_name="difference_reports")
    op.drop_table("difference_reports")
    op.drop_index("ix_quality_runs_version", table_name="quality_runs")
    op.drop_table("quality_runs")
    op.drop_index("ix_dataset_versions_dataset_status", table_name="dataset_versions")
    op.drop_table("dataset_versions")
    op.drop_index("ix_ingestion_batches_dataset_status", table_name="ingestion_batches")
    op.drop_table("ingestion_batches")
