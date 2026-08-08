"""Retire the S1-07 institutional evidence write gate without breaking history.

Revision ID: 20260808_0013
Revises: 20260729_0012
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0013"
down_revision: str | None = "20260729_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Restore the simple Stage 1 assessment repository contract.

    The retired seal columns remain in place so an already-migrated personal
    database keeps its historical bytes. New rows may leave those columns
    empty and no longer require a process-local cryptographic write gate.
    """
    op.execute("DROP TRIGGER IF EXISTS source_assessments_r5_immutable_delete")
    op.execute("DROP TRIGGER IF EXISTS source_assessments_r5_immutable_update")
    op.execute("DROP TRIGGER IF EXISTS source_assessments_r5_write_gate")
    with op.batch_alter_table("source_candidate_assessments") as batch:
        batch.alter_column(
            "sealed_evidence_relative_path",
            existing_type=sa.Text(),
            nullable=True,
        )
        batch.alter_column(
            "sealed_evidence_sha256",
            existing_type=sa.String(length=64),
            nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    incompatible = int(
        bind.exec_driver_sql(
            "SELECT count(*) FROM source_candidate_assessments "
            "WHERE sealed_evidence_relative_path IS NULL "
            "OR sealed_evidence_sha256 IS NULL"
        ).scalar()
    )
    if incompatible:
        raise RuntimeError(
            "cannot restore the retired evidence gate after simple assessment rows exist"
        )
    with op.batch_alter_table("source_candidate_assessments") as batch:
        batch.alter_column(
            "sealed_evidence_relative_path",
            existing_type=sa.Text(),
            nullable=False,
        )
        batch.alter_column(
            "sealed_evidence_sha256",
            existing_type=sa.String(length=64),
            nullable=False,
        )
    _create_r5_triggers()


def _create_r5_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER source_assessments_r5_write_gate
        BEFORE INSERT ON source_candidate_assessments
        WHEN quantking_assessment_write_gate(
          NEW.assessment_id, NEW.source, NEW.dataset, NEW.evidence_kind,
          NEW.semantic_contract_verified, NEW.schema_unit_verified,
          NEW.coverage_adequate, NEW.quality_passed,
          NEW.fixed_sample_differences_adequate, NEW.stability_runs,
          NEW.status, NEW.blocking_reason, NEW.assessment_config_version,
          NEW.sample_spec_version, NEW.rule_version, NEW.evidence_relative_path,
          NEW.evidence_sha256, NEW.signer_profiles_json,
          NEW.result_attestation_sha256s_json, NEW.created_at,
          NEW.persistence_trust_status, NEW.sealed_evidence_relative_path,
          NEW.sealed_evidence_sha256
        ) <> 1
        BEGIN
          SELECT RAISE(ABORT, 'assessment write requires an active sealed-evidence gate');
        END
        """
    )
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER source_assessments_r5_immutable_{action.lower()}
            BEFORE {action} ON source_candidate_assessments
            BEGIN
              SELECT RAISE(ABORT, 'source assessment rows are immutable');
            END
            """
        )
