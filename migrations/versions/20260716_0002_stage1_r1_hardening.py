"""Harden Stage 1 metadata lineage, immutability, and override bindings.

Revision ID: 20260716_0002
Revises: 20260716_0001
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260716_0002"
down_revision: str | None = "20260716_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_overrides = bind.execute(sa.text("SELECT COUNT(*) FROM source_overrides")).scalar_one()
    if existing_overrides:
        raise RuntimeError(
            "S1-01-R1 requires source_overrides to be empty before adding mandatory lineage"
        )

    with op.batch_alter_table("ingestion_batches") as batch:
        batch.drop_constraint("uq_batches_source_dataset_params", type_="unique")
        batch.add_column(sa.Column("empty_result_json", sa.Text()))

    with op.batch_alter_table("dataset_versions") as batch:
        batch.add_column(sa.Column("coverage_start", sa.String(length=10)))
        batch.add_column(sa.Column("coverage_end", sa.String(length=10)))
        batch.add_column(
            sa.Column(
                "coverage_summary_json",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch.create_foreign_key(
            "fk_dataset_versions_parent",
            "dataset_versions",
            ["parent_version_id"],
            ["version_id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_dataset_versions_coverage_dates",
            "(coverage_start IS NULL AND coverage_end IS NULL) OR "
            "(coverage_start IS NOT NULL AND coverage_end IS NOT NULL "
            "AND coverage_start <= coverage_end)",
        )

    op.create_table(
        "dataset_version_batches",
        sa.Column(
            "version_id",
            sa.String(length=96),
            sa.ForeignKey("dataset_versions.version_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "batch_id",
            sa.String(length=96),
            sa.ForeignKey("ingestion_batches.batch_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_dataset_version_batches_ordinal"),
        sa.UniqueConstraint("version_id", "ordinal", name="uq_dataset_version_batches_ordinal"),
    )
    op.create_index("ix_dataset_version_batches_batch", "dataset_version_batches", ["batch_id"])

    op.create_table(
        "quality_results",
        sa.Column(
            "run_id",
            sa.String(length=96),
            sa.ForeignKey("quality_runs.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sequence_number", sa.Integer(), primary_key=True),
        sa.Column("rule_code", sa.String(length=128), nullable=False),
        sa.Column("passed", sa.Integer(), nullable=False),
        sa.Column("affected_count", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("report_relative_path", sa.Text()),
        sa.CheckConstraint("sequence_number >= 0", name="ck_quality_results_sequence"),
        sa.CheckConstraint("passed IN (0,1)", name="ck_quality_results_passed"),
        sa.CheckConstraint("affected_count >= 0", name="ck_quality_results_affected_count"),
        sa.UniqueConstraint("run_id", "rule_code", name="uq_quality_results_rule_code"),
    )

    op.execute("DROP TRIGGER IF EXISTS source_overrides_no_update_applied")
    with op.batch_alter_table("source_overrides") as batch:
        batch.add_column(sa.Column("difference_report_id", sa.String(length=96), nullable=False))
        batch.add_column(sa.Column("primary_batch_id", sa.String(length=96), nullable=False))
        batch.add_column(sa.Column("fallback_batch_id", sa.String(length=96), nullable=False))
        batch.create_foreign_key(
            "fk_source_overrides_difference_report",
            "difference_reports",
            ["difference_report_id"],
            ["report_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_source_overrides_primary_batch",
            "ingestion_batches",
            ["primary_batch_id"],
            ["batch_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_source_overrides_fallback_batch",
            "ingestion_batches",
            ["fallback_batch_id"],
            ["batch_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_source_overrides_applied_version",
            "dataset_versions",
            ["applied_version_id"],
            ["version_id"],
            ondelete="RESTRICT",
        )

    op.execute(
        "CREATE TRIGGER ingestion_batches_no_update_terminal "
        "BEFORE UPDATE ON ingestion_batches "
        "WHEN OLD.status IN ('SUCCEEDED','PARTIAL','FAILED') "
        "BEGIN SELECT RAISE(ABORT, 'terminal ingestion batches are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER ingestion_batches_no_delete_terminal "
        "BEFORE DELETE ON ingestion_batches "
        "WHEN OLD.status IN ('SUCCEEDED','PARTIAL','FAILED') "
        "BEGIN SELECT RAISE(ABORT, 'terminal ingestion batches are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER dataset_versions_no_update_failed BEFORE UPDATE ON dataset_versions "
        "WHEN OLD.status = 'FAILED' "
        "BEGIN SELECT RAISE(ABORT, 'failed dataset versions are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER dataset_versions_no_delete_failed BEFORE DELETE ON dataset_versions "
        "WHEN OLD.status = 'FAILED' "
        "BEGIN SELECT RAISE(ABORT, 'failed dataset versions are immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER dataset_versions_parent_dataset_insert "
        "BEFORE INSERT ON dataset_versions WHEN NEW.parent_version_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM dataset_versions p WHERE p.version_id=NEW.parent_version_id "
        "AND p.dataset=NEW.dataset) "
        "BEGIN SELECT RAISE(ABORT, 'parent dataset version must exist in the same dataset'); END"
    )
    op.execute(
        "CREATE TRIGGER dataset_version_batches_dataset_insert "
        "BEFORE INSERT ON dataset_version_batches WHEN NOT EXISTS "
        "(SELECT 1 FROM dataset_versions v JOIN ingestion_batches b "
        "ON b.batch_id=NEW.batch_id WHERE v.version_id=NEW.version_id "
        "AND v.dataset=b.dataset) "
        "BEGIN SELECT RAISE(ABORT, 'input batch must belong to the version dataset'); END"
    )
    op.execute(
        "CREATE TRIGGER source_overrides_validate_insert BEFORE INSERT ON source_overrides "
        "WHEN NEW.status <> 'PENDING' OR NEW.primary_batch_id=NEW.fallback_batch_id OR NOT EXISTS "
        "(SELECT 1 FROM ingestion_batches p, ingestion_batches f, difference_reports d, "
        "dataset_versions v WHERE p.batch_id=NEW.primary_batch_id "
        "AND p.source=NEW.primary_source AND p.dataset=NEW.dataset "
        "AND f.batch_id=NEW.fallback_batch_id AND f.source=NEW.fallback_source "
        "AND f.dataset=NEW.dataset AND d.report_id=NEW.difference_report_id "
        "AND d.primary_source=NEW.primary_source AND d.fallback_source=NEW.fallback_source "
        "AND v.version_id=d.version_id AND v.dataset=NEW.dataset) "
        "BEGIN SELECT RAISE(ABORT, 'source override lineage is inconsistent'); END"
    )
    op.execute(
        "CREATE TRIGGER source_overrides_pending_transition BEFORE UPDATE ON source_overrides "
        "WHEN OLD.status='PENDING' AND (NEW.status NOT IN ('APPROVED','REJECTED') "
        "OR NEW.override_id IS NOT OLD.override_id OR NEW.dataset IS NOT OLD.dataset "
        "OR NEW.difference_report_id IS NOT OLD.difference_report_id "
        "OR NEW.primary_batch_id IS NOT OLD.primary_batch_id "
        "OR NEW.fallback_batch_id IS NOT OLD.fallback_batch_id "
        "OR NEW.primary_source IS NOT OLD.primary_source "
        "OR NEW.fallback_source IS NOT OLD.fallback_source "
        "OR NEW.scope_codes_json IS NOT OLD.scope_codes_json "
        "OR NEW.scope_dates_json IS NOT OLD.scope_dates_json "
        "OR NEW.fields_json IS NOT OLD.fields_json OR NEW.reason IS NOT OLD.reason "
        "OR NEW.created_at IS NOT OLD.created_at OR NEW.approved_at IS NULL "
        "OR NEW.approved_by IS NULL OR NEW.applied_at IS NOT NULL "
        "OR NEW.applied_version_id IS NOT NULL) "
        "BEGIN SELECT RAISE(ABORT, 'invalid source override decision transition'); END"
    )
    op.execute(
        "CREATE TRIGGER source_overrides_approved_transition BEFORE UPDATE ON source_overrides "
        "WHEN OLD.status='APPROVED' AND (NEW.status <> 'APPLIED' "
        "OR NEW.override_id IS NOT OLD.override_id OR NEW.dataset IS NOT OLD.dataset "
        "OR NEW.difference_report_id IS NOT OLD.difference_report_id "
        "OR NEW.primary_batch_id IS NOT OLD.primary_batch_id "
        "OR NEW.fallback_batch_id IS NOT OLD.fallback_batch_id "
        "OR NEW.primary_source IS NOT OLD.primary_source "
        "OR NEW.fallback_source IS NOT OLD.fallback_source "
        "OR NEW.scope_codes_json IS NOT OLD.scope_codes_json "
        "OR NEW.scope_dates_json IS NOT OLD.scope_dates_json "
        "OR NEW.fields_json IS NOT OLD.fields_json OR NEW.reason IS NOT OLD.reason "
        "OR NEW.created_at IS NOT OLD.created_at OR NEW.approved_at IS NOT OLD.approved_at "
        "OR NEW.approved_by IS NOT OLD.approved_by OR NEW.applied_at IS NULL "
        "OR NEW.applied_version_id IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM dataset_versions v JOIN difference_reports d "
        "ON d.report_id=OLD.difference_report_id WHERE v.version_id=NEW.applied_version_id "
        "AND v.dataset=OLD.dataset AND v.version_id<>d.version_id)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'approved override may only bind a new same-dataset version'); END"
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


def downgrade() -> None:
    for trigger in (
        "source_overrides_no_delete_decided",
        "source_overrides_no_update_terminal",
        "source_overrides_approved_transition",
        "source_overrides_pending_transition",
        "source_overrides_validate_insert",
        "dataset_version_batches_dataset_insert",
        "dataset_versions_parent_dataset_insert",
        "dataset_versions_no_delete_failed",
        "dataset_versions_no_update_failed",
        "ingestion_batches_no_delete_terminal",
        "ingestion_batches_no_update_terminal",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")

    with op.batch_alter_table("source_overrides") as batch:
        batch.drop_constraint("fk_source_overrides_applied_version", type_="foreignkey")
        batch.drop_constraint("fk_source_overrides_fallback_batch", type_="foreignkey")
        batch.drop_constraint("fk_source_overrides_primary_batch", type_="foreignkey")
        batch.drop_constraint("fk_source_overrides_difference_report", type_="foreignkey")
        batch.drop_column("fallback_batch_id")
        batch.drop_column("primary_batch_id")
        batch.drop_column("difference_report_id")

    op.execute(
        "CREATE TRIGGER source_overrides_no_update_applied BEFORE UPDATE ON source_overrides "
        "WHEN OLD.status = 'APPLIED' "
        "BEGIN SELECT RAISE(ABORT, 'applied source overrides are immutable'); END"
    )
    op.drop_table("quality_results")
    op.drop_index("ix_dataset_version_batches_batch", table_name="dataset_version_batches")
    op.drop_table("dataset_version_batches")

    with op.batch_alter_table("dataset_versions") as batch:
        batch.drop_constraint("ck_dataset_versions_coverage_dates", type_="check")
        batch.drop_constraint("fk_dataset_versions_parent", type_="foreignkey")
        batch.drop_column("coverage_summary_json")
        batch.drop_column("coverage_end")
        batch.drop_column("coverage_start")

    with op.batch_alter_table("ingestion_batches") as batch:
        batch.drop_column("empty_result_json")
        batch.create_unique_constraint(
            "uq_batches_source_dataset_params",
            ["source", "dataset", "request_params_sha256"],
        )
