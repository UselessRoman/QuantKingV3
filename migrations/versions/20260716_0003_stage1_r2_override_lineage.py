"""Enforce applied override direct-parent lineage at the database boundary.

Revision ID: 20260716_0003
Revises: 20260716_0002
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260716_0003"
down_revision: str | None = "20260716_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP TRIGGER source_overrides_approved_transition")
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
        "OR NEW.applied_at < OLD.approved_at OR NEW.applied_version_id IS NULL "
        "OR NOT EXISTS (SELECT 1 FROM dataset_versions v JOIN difference_reports d "
        "ON d.report_id=OLD.difference_report_id WHERE v.version_id=NEW.applied_version_id "
        "AND v.dataset=OLD.dataset AND v.version_id<>d.version_id "
        "AND v.parent_version_id=d.version_id AND v.created_at>=OLD.approved_at)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'approved override requires a new direct child of the compared version'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER source_overrides_approved_transition")
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
