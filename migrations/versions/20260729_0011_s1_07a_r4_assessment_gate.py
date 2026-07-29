"""Restrict S1-07A assessment rows to immutable synthetic blocked evidence.

Revision ID: 20260729_0011
Revises: 20260729_0010
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260729_0011"
down_revision: str | None = "20260729_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TRIGGER source_assessments_s1_07a_insert_gate
        BEFORE INSERT ON source_candidate_assessments
        WHEN NEW.evidence_kind <> 'SYNTHETIC'
          OR NEW.status <> 'BLOCKED_EXTERNAL_VALIDATION'
          OR json_valid(NEW.signer_profiles_json) = 0
          OR NEW.signer_profiles_json <> '["SYNTHETIC"]'
          OR json_valid(NEW.result_attestation_sha256s_json) = 0
          OR CASE
               WHEN json_valid(NEW.result_attestation_sha256s_json)
               THEN json_array_length(NEW.result_attestation_sha256s_json) < 1
               ELSE 1
             END
        BEGIN
          SELECT RAISE(ABORT, 'S1-07A assessment rows require synthetic blocked evidence');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_assessments_s1_07a_immutable_update
        BEFORE UPDATE ON source_candidate_assessments
        BEGIN
          SELECT RAISE(ABORT, 'source assessment rows are immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_assessments_s1_07a_immutable_delete
        BEFORE DELETE ON source_candidate_assessments
        BEGIN
          SELECT RAISE(ABORT, 'source assessment rows are immutable');
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS source_assessments_s1_07a_immutable_delete")
    op.execute("DROP TRIGGER IF EXISTS source_assessments_s1_07a_immutable_update")
    op.execute("DROP TRIGGER IF EXISTS source_assessments_s1_07a_insert_gate")
