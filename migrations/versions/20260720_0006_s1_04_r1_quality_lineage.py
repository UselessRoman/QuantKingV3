"""Close S1-04 quality lineage and source assessment gaps.

Revision ID: 20260720_0006
Revises: 20260716_0005
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260720_0006"
down_revision: str | None = "20260716_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS dataset_versions_quality_gate_before_publish")
    op.execute("DROP TRIGGER IF EXISTS source_overrides_approved_transition")
    # SQLite's ALTER TABLE ADD COLUMN is sufficient here. A foreign key is
    # deliberately enforced by the publish trigger because adding one would
    # require recreating dataset_versions while older lineage triggers exist.
    op.add_column(
        "dataset_versions", sa.Column("quality_run_id", sa.String(length=96), nullable=True)
    )
    op.add_column(
        "dataset_versions", sa.Column("content_sha256", sa.String(length=64), nullable=True)
    )
    op.create_table(
        "source_candidate_assessments",
        sa.Column("assessment_id", sa.String(length=96), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("dataset", sa.String(length=64), nullable=False),
        sa.Column("evidence_kind", sa.String(length=16), nullable=False),
        sa.Column("semantic_contract_verified", sa.Integer(), nullable=False),
        sa.Column("schema_unit_verified", sa.Integer(), nullable=False),
        sa.Column("coverage_adequate", sa.Integer(), nullable=False),
        sa.Column("quality_passed", sa.Integer(), nullable=False),
        sa.Column("fixed_sample_differences_adequate", sa.Integer(), nullable=False),
        sa.Column("stability_runs", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("blocking_reason", sa.Text(), nullable=True),
        sa.Column("assessment_config_version", sa.String(length=128), nullable=False),
        sa.Column("sample_spec_version", sa.String(length=128), nullable=False),
        sa.Column("rule_version", sa.String(length=128), nullable=False),
        sa.Column("evidence_relative_path", sa.Text(), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.CheckConstraint("evidence_kind IN ('SYNTHETIC','REAL')", name="ck_assessment_evidence"),
        sa.CheckConstraint(
            "status IN ('UNVERIFIED','BLOCKED_EXTERNAL_VALIDATION','PRODUCTION_READY')",
            name="ck_assessment_status",
        ),
        sa.CheckConstraint("stability_runs >= 0", name="ck_assessment_stability_runs"),
    )
    op.create_index(
        "ix_assessments_source_dataset",
        "source_candidate_assessments",
        ["source", "dataset", "created_at"],
    )
    op.execute(
        "CREATE TRIGGER dataset_versions_quality_gate_before_publish "
        "BEFORE UPDATE ON dataset_versions "
        "WHEN NEW.status='PUBLISHED' AND OLD.status<>'PUBLISHED' AND NOT EXISTS "
        "(SELECT 1 FROM quality_runs q WHERE q.run_id=NEW.quality_run_id "
        "AND q.version_id=NEW.version_id AND q.status='PASSED' "
        "AND q.rule_version=NEW.rule_version AND q.schema_version=NEW.schema_version "
        "AND q.content_sha256=NEW.content_sha256) "
        "BEGIN SELECT RAISE(ABORT, 'PUBLISHED version requires exact PASSED quality run'); END"
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
        "OR NEW.applied_version_id IS NULL "
        "OR NOT EXISTS (SELECT 1 FROM dataset_versions v JOIN difference_reports d "
        "ON d.report_id=OLD.difference_report_id WHERE v.version_id=NEW.applied_version_id "
        "AND v.dataset=OLD.dataset AND v.status='PUBLISHED' AND v.version_id<>d.version_id "
        "AND v.parent_version_id=d.primary_version_id AND v.created_at>=OLD.approved_at)) "
        "BEGIN SELECT RAISE(ABORT, 'approved override requires a published direct child'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS source_overrides_approved_transition")
    op.execute("DROP TRIGGER IF EXISTS dataset_versions_quality_gate_before_publish")
    op.drop_index("ix_assessments_source_dataset", table_name="source_candidate_assessments")
    op.drop_table("source_candidate_assessments")
    op.drop_column("dataset_versions", "content_sha256")
    op.drop_column("dataset_versions", "quality_run_id")
    # Restore the 20260716_0004 quality gate trigger (without content_sha256).
    op.execute(
        "CREATE TRIGGER dataset_versions_quality_gate_before_publish "
        "BEFORE UPDATE ON dataset_versions "
        "WHEN NEW.status='PUBLISHED' AND OLD.status<>'PUBLISHED' AND NOT EXISTS "
        "(SELECT 1 FROM quality_runs q WHERE q.version_id=NEW.version_id AND q.status='PASSED') "
        "BEGIN SELECT RAISE(ABORT, 'PUBLISHED dataset version requires a PASSED quality run'); END"
    )
    # Restore the 20260716_0005 override approved_transition trigger
    # (with primary_version_id, not d.version_id, in the direct-parent check).
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
        "AND v.dataset=OLD.dataset AND v.version_id<>d.primary_version_id "
        "AND v.parent_version_id=d.primary_version_id AND v.created_at>=OLD.approved_at)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'approved override requires a new direct child of the compared version'); END"
    )
