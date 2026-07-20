"""Add quality gate columns and PUBLISHED transition trigger (S1-04A).

Revision ID: 20260716_0004
Revises: 20260716_0003
Create Date: 2026-07-20

Adds ``schema_version`` and ``content_sha256`` columns to ``quality_runs``
so a PUBLISHED transition can be proven to bind to the exact candidate
content the quality run validated. Adds a SQLite trigger that blocks any
direct ``UPDATE dataset_versions SET status='PUBLISHED'`` when no
matching PASSED ``quality_runs`` row exists for the same ``version_id``.

The trigger complements the application-level
:class:`CanonicalPublishCoordinator` quality gate: even a direct SQL
UPDATE or a repository call that bypasses the coordinator cannot mark a
version PUBLISHED without a PASSED quality run on file.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260716_0004"
down_revision: str | None = "20260716_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_quality_runs = bind.execute(sa.text("SELECT COUNT(*) FROM quality_runs")).scalar_one()
    if existing_quality_runs:
        raise RuntimeError(
            "S1-04 quality gate requires quality_runs to be empty before adding "
            "mandatory schema_version and content_sha256 columns"
        )

    with op.batch_alter_table("quality_runs") as batch:
        batch.add_column(
            sa.Column(
                "schema_version",
                sa.String(length=128),
                nullable=False,
                server_default=sa.text("''"),
            )
        )
        batch.add_column(
            sa.Column(
                "content_sha256",
                sa.String(length=64),
                nullable=False,
                server_default=sa.text("'0' * 64"),
            )
        )
        # Drop the placeholder server defaults so future inserts must
        # supply real values; the NOT NULL constraint remains.
        batch.alter_column("schema_version", server_default=None)
        batch.alter_column("content_sha256", server_default=None)

    # Trigger: a PUBLISHED transition requires a PASSED quality run bound
    # to the same version_id. The trigger fires BEFORE UPDATE on
    # dataset_versions when the new status is PUBLISHED and the old
    # status is not PUBLISHED (idempotent re-coordination of an already
    # PUBLISHED row is allowed because the no-update-published trigger
    # would already have blocked any mutation).
    op.execute(
        "CREATE TRIGGER dataset_versions_quality_gate_before_publish "
        "BEFORE UPDATE ON dataset_versions "
        "WHEN NEW.status='PUBLISHED' AND OLD.status<>'PUBLISHED' AND NOT EXISTS "
        "(SELECT 1 FROM quality_runs q WHERE q.version_id=NEW.version_id "
        "AND q.status='PASSED') "
        "BEGIN SELECT RAISE(ABORT, "
        "'PUBLISHED dataset version requires a PASSED quality run'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS dataset_versions_quality_gate_before_publish")
    with op.batch_alter_table("quality_runs") as batch:
        batch.drop_column("content_sha256")
        batch.drop_column("schema_version")
