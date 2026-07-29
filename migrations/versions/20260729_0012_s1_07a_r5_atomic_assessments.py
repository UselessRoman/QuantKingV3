"""Quarantine legacy assessments and require a connection-local write gate.

Revision ID: 20260729_0012
Revises: 20260729_0011
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260729_0012"
down_revision: str | None = "20260729_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ASSESSMENT_COLUMNS = (
    "assessment_id",
    "source",
    "dataset",
    "evidence_kind",
    "semantic_contract_verified",
    "schema_unit_verified",
    "coverage_adequate",
    "quality_passed",
    "fixed_sample_differences_adequate",
    "stability_runs",
    "status",
    "blocking_reason",
    "assessment_config_version",
    "sample_spec_version",
    "rule_version",
    "evidence_relative_path",
    "evidence_sha256",
    "signer_profiles_json",
    "result_attestation_sha256s_json",
    "created_at",
)


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS source_assessments_s1_07a_immutable_delete")
    op.execute("DROP TRIGGER IF EXISTS source_assessments_s1_07a_immutable_update")
    op.execute("DROP TRIGGER IF EXISTS source_assessments_s1_07a_insert_gate")
    op.create_table(
        "source_candidate_assessment_quarantine",
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
        sa.Column("signer_profiles_json", sa.Text(), nullable=False),
        sa.Column("result_attestation_sha256s_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("quarantined_from_revision", sa.String(length=32), nullable=False),
        sa.Column("quarantine_reason", sa.String(length=96), nullable=False),
    )
    columns = ",".join(_ASSESSMENT_COLUMNS)
    op.execute(
        "INSERT INTO source_candidate_assessment_quarantine "
        f"({columns},quarantined_from_revision,quarantine_reason) "
        f"SELECT {columns},'20260729_0011','PRE_R5_UNVERIFIED_WRITE_PATH' "
        "FROM source_candidate_assessments"
    )
    op.execute("DELETE FROM source_candidate_assessments")
    op.add_column(
        "source_candidate_assessments",
        sa.Column(
            "persistence_trust_status",
            sa.String(length=40),
            nullable=False,
            server_default="SEALED_VERIFIED_V1",
        ),
    )
    op.add_column(
        "source_candidate_assessments",
        sa.Column("sealed_evidence_relative_path", sa.Text(), nullable=False),
    )
    op.add_column(
        "source_candidate_assessments",
        sa.Column("sealed_evidence_sha256", sa.String(length=64), nullable=False),
    )
    _create_r5_triggers()
    _create_quarantine_triggers()


def downgrade() -> None:
    bind = op.get_bind()
    active = int(bind.exec_driver_sql("SELECT count(*) FROM source_candidate_assessments").scalar())
    quarantined = int(
        bind.exec_driver_sql("SELECT count(*) FROM source_candidate_assessment_quarantine").scalar()
    )
    if active or quarantined:
        raise RuntimeError(
            "assessment downgrade is blocked while verified or quarantined evidence exists"
        )
    op.execute("DROP TRIGGER IF EXISTS source_assessment_quarantine_no_delete")
    op.execute("DROP TRIGGER IF EXISTS source_assessment_quarantine_no_update")
    op.execute("DROP TRIGGER IF EXISTS source_assessment_quarantine_no_insert")
    op.execute("DROP TRIGGER IF EXISTS source_assessments_r5_immutable_delete")
    op.execute("DROP TRIGGER IF EXISTS source_assessments_r5_immutable_update")
    op.execute("DROP TRIGGER IF EXISTS source_assessments_r5_write_gate")
    op.drop_column("source_candidate_assessments", "sealed_evidence_sha256")
    op.drop_column("source_candidate_assessments", "sealed_evidence_relative_path")
    op.drop_column("source_candidate_assessments", "persistence_trust_status")
    op.drop_table("source_candidate_assessment_quarantine")
    _create_r4_triggers()


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
    op.execute(
        """
        CREATE TRIGGER source_assessments_r5_immutable_update
        BEFORE UPDATE ON source_candidate_assessments
        BEGIN
          SELECT RAISE(ABORT, 'source assessment rows are immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_assessments_r5_immutable_delete
        BEFORE DELETE ON source_candidate_assessments
        BEGIN
          SELECT RAISE(ABORT, 'source assessment rows are immutable');
        END
        """
    )


def _create_quarantine_triggers() -> None:
    for action in ("INSERT", "UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER source_assessment_quarantine_no_{action.lower()}
            BEFORE {action} ON source_candidate_assessment_quarantine
            BEGIN
              SELECT RAISE(ABORT, 'assessment quarantine is immutable');
            END
            """
        )


def _create_r4_triggers() -> None:
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
