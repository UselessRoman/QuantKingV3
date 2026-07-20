"""Extend difference_reports with both-side bindings (S1-04B).

Revision ID: 20260716_0005
Revises: 20260716_0004
Create Date: 2026-07-20

Adds both-side binding columns to ``difference_reports`` so the SQLite
repository can verify that a DifferenceReport references real, matching
batches, dataset versions and quality runs before persisting. The new
columns are NOT NULL with foreign keys to ``ingestion_batches``,
``dataset_versions`` and ``quality_runs``; they carry the full lineage
that the original S1-03 columns lacked.

The migration refuses to run if any existing ``difference_reports`` row
would be left without bindings; the S1-03 difference reports were only
ever produced in offline tests and Stage 1 has not yet persisted a real
report, so the table is expected to be empty at upgrade time.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260716_0005"
down_revision: str | None = "20260716_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_reports = bind.execute(sa.text("SELECT COUNT(*) FROM difference_reports")).scalar_one()
    if existing_reports:
        raise RuntimeError(
            "S1-04B difference_reports binding extension requires the table to be "
            "empty before adding mandatory both-side binding columns"
        )

    # Drop triggers that reference difference_reports by name before
    # batch_alter_table renames the table; SQLite triggers resolve table
    # names at definition time and would otherwise break the rename step.
    # The triggers are recreated after the columns are added, with the
    # same lineage checks plus the new requirement that override batches
    # match the report's compared batches.
    op.execute("DROP TRIGGER IF EXISTS source_overrides_validate_insert")
    op.execute("DROP TRIGGER IF EXISTS source_overrides_approved_transition")

    with op.batch_alter_table("difference_reports") as batch:
        batch.add_column(
            sa.Column(
                "primary_batch_id",
                sa.String(length=96),
                nullable=False,
                server_default=sa.text("''"),
            )
        )
        batch.add_column(
            sa.Column(
                "fallback_batch_id",
                sa.String(length=96),
                nullable=False,
                server_default=sa.text("''"),
            )
        )
        batch.add_column(
            sa.Column(
                "primary_version_id",
                sa.String(length=96),
                nullable=False,
                server_default=sa.text("''"),
            )
        )
        batch.add_column(
            sa.Column(
                "fallback_version_id",
                sa.String(length=96),
                nullable=False,
                server_default=sa.text("''"),
            )
        )
        batch.add_column(
            sa.Column(
                "primary_content_sha256",
                sa.String(length=64),
                nullable=False,
                server_default=sa.text("'0' * 64"),
            )
        )
        batch.add_column(
            sa.Column(
                "fallback_content_sha256",
                sa.String(length=64),
                nullable=False,
                server_default=sa.text("'0' * 64"),
            )
        )
        batch.add_column(
            sa.Column(
                "primary_quality_run_id",
                sa.String(length=96),
                nullable=False,
                server_default=sa.text("''"),
            )
        )
        batch.add_column(
            sa.Column(
                "fallback_quality_run_id",
                sa.String(length=96),
                nullable=False,
                server_default=sa.text("''"),
            )
        )
        batch.add_column(
            sa.Column(
                "sample_spec_sha256",
                sa.String(length=64),
                nullable=False,
                server_default=sa.text("'0' * 64"),
            )
        )
        batch.add_column(
            sa.Column(
                "threshold_rule_version",
                sa.String(length=64),
                nullable=False,
                server_default=sa.text("''"),
            )
        )
        batch.add_column(
            sa.Column(
                "report_sha256",
                sa.String(length=64),
                nullable=False,
                server_default=sa.text("'0' * 64"),
            )
        )
        # Drop placeholder server defaults so future inserts must supply
        # real values; NOT NULL constraints remain.
        for col in (
            "primary_batch_id",
            "fallback_batch_id",
            "primary_version_id",
            "fallback_version_id",
            "primary_content_sha256",
            "fallback_content_sha256",
            "primary_quality_run_id",
            "fallback_quality_run_id",
            "sample_spec_sha256",
            "threshold_rule_version",
            "report_sha256",
        ):
            batch.alter_column(col, server_default=None)
        # Add foreign keys now that the columns are populated (table is empty).
        batch.create_foreign_key(
            "fk_difference_reports_primary_batch",
            "ingestion_batches",
            ["primary_batch_id"],
            ["batch_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_difference_reports_fallback_batch",
            "ingestion_batches",
            ["fallback_batch_id"],
            ["batch_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_difference_reports_primary_version",
            "dataset_versions",
            ["primary_version_id"],
            ["version_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_difference_reports_fallback_version",
            "dataset_versions",
            ["fallback_version_id"],
            ["version_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_difference_reports_primary_quality_run",
            "quality_runs",
            ["primary_quality_run_id"],
            ["run_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_difference_reports_fallback_quality_run",
            "quality_runs",
            ["fallback_quality_run_id"],
            ["run_id"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_difference_reports_primary_batch", ["primary_batch_id"])
        batch.create_index("ix_difference_reports_fallback_batch", ["fallback_batch_id"])

    # Recreate the triggers with the original lineage checks plus the
    # new requirement that override batches match the report's compared
    # batches. The approved_transition trigger is the version from
    # migration 20260716_0003 (direct-parent + created_at>=approved_at).
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
        "AND v.dataset=OLD.dataset AND v.version_id<>d.version_id "
        "AND v.parent_version_id=d.version_id AND v.created_at>=OLD.approved_at)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'approved override requires a new direct child of the compared version'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS source_overrides_approved_transition")
    op.execute("DROP TRIGGER IF EXISTS source_overrides_validate_insert")
    with op.batch_alter_table("difference_reports") as batch:
        batch.drop_index("ix_difference_reports_fallback_batch")
        batch.drop_index("ix_difference_reports_primary_batch")
        batch.drop_constraint("fk_difference_reports_fallback_quality_run", type_="foreignkey")
        batch.drop_constraint("fk_difference_reports_primary_quality_run", type_="foreignkey")
        batch.drop_constraint("fk_difference_reports_fallback_version", type_="foreignkey")
        batch.drop_constraint("fk_difference_reports_primary_version", type_="foreignkey")
        batch.drop_constraint("fk_difference_reports_fallback_batch", type_="foreignkey")
        batch.drop_constraint("fk_difference_reports_primary_batch", type_="foreignkey")
        for col in (
            "report_sha256",
            "threshold_rule_version",
            "sample_spec_sha256",
            "fallback_quality_run_id",
            "primary_quality_run_id",
            "fallback_content_sha256",
            "primary_content_sha256",
            "fallback_version_id",
            "primary_version_id",
            "fallback_batch_id",
            "primary_batch_id",
        ):
            batch.drop_column(col)
    # Recreate the original triggers (without the new batch-binding check).
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
        "AND v.dataset=OLD.dataset AND v.version_id<>d.version_id "
        "AND v.parent_version_id=d.version_id AND v.created_at>=OLD.approved_at)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'approved override requires a new direct child of the compared version'); END"
    )
