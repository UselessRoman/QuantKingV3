"""Bind persisted assessments to signer and result-attestation summaries.

Revision ID: 20260729_0010
Revises: 20260722_0009
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260729_0010"
down_revision: str | None = "20260722_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source_candidate_assessments",
        sa.Column("signer_profiles_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "source_candidate_assessments",
        sa.Column(
            "result_attestation_sha256s_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("source_candidate_assessments", "result_attestation_sha256s_json")
    op.drop_column("source_candidate_assessments", "signer_profiles_json")
