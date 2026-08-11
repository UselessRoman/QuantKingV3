"""SQLAlchemy ORM rows for Stage 1 data metadata, separate from domain models.

These rows deliberately live under ``data/adapters/persistence`` (the only
location where SQLAlchemy/Alembic imports are permitted by the architecture
boundary checker). They are mapped to and from the framework-free domain objects
in :mod:`quantking.data.models` at the repository boundary; they are never
exposed to web/API schemas or application services.

Parquet row data is never stored in SQLite: only summaries, counts, managed
report identifiers and manifest SHA-256 hashes are persisted here. Large
row-level quality differences live in managed report files referenced by a
relative identifier.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from quantking.jobs.adapters.persistence.models import JobRunRow


class Base(DeclarativeBase):
    """Stage 1 data metadata persistence metadata, separate from the jobs Base."""


# ResolutionRun has a real JobRun FK. Copy the complete jobs table metadata so
# SQLAlchemy create_all and Alembic-backed schemas enforce the same relationship.
cast(Table, JobRunRow.__table__).to_metadata(Base.metadata)


class IngestionBatchRow(Base):
    __tablename__ = "ingestion_batches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','PARTIAL','FAILED')",
            name="ck_ingestion_batches_status",
        ),
        CheckConstraint(
            "row_count IS NULL OR row_count >= 0", name="ck_ingestion_batches_row_count"
        ),
        CheckConstraint(
            "security_count IS NULL OR security_count >= 0",
            name="ck_ingestion_batches_security_count",
        ),
        Index("ix_ingestion_batches_dataset_status", "dataset", "status"),
    )

    batch_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(128), nullable=False)
    request_params_json: Mapped[str] = mapped_column(Text, nullable=False)
    request_params_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[str] = mapped_column(String(40), nullable=False)
    ended_at: Mapped[str | None] = mapped_column(String(40))
    row_count: Mapped[int | None] = mapped_column(Integer)
    security_count: Mapped[int | None] = mapped_column(Integer)
    manifest_files_json: Mapped[str] = mapped_column(Text, nullable=False)
    failure_json: Mapped[str | None] = mapped_column(Text)
    empty_result_json: Mapped[str | None] = mapped_column(Text)
    # Plain string, not a SQLAlchemy ForeignKey: the real FK constraint is created
    # by the Alembic migration against job_runs.job_id. Keeping it a plain column
    # avoids coupling the data ORM metadata to the jobs ORM metadata, so each
    # bounded context's Base can be used independently for autogenerate/mapping.
    job_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DatasetVersionRow(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('BUILDING','VALIDATED','PUBLISHED','FAILED')",
            name="ck_dataset_versions_status",
        ),
        CheckConstraint(
            "security_count IS NULL OR security_count >= 0",
            name="ck_dataset_versions_security_count",
        ),
        CheckConstraint(
            "row_count IS NULL OR row_count >= 0", name="ck_dataset_versions_row_count"
        ),
        Index("ix_dataset_versions_dataset_status", "dataset", "status"),
    )

    version_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    code_version: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("dataset_versions.version_id", ondelete="RESTRICT")
    )
    input_batch_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    validated_at: Mapped[str | None] = mapped_column(String(40))
    published_at: Mapped[str | None] = mapped_column(String(40))
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    security_count: Mapped[int | None] = mapped_column(Integer)
    row_count: Mapped[int | None] = mapped_column(Integer)
    coverage_start: Mapped[str | None] = mapped_column(String(10))
    coverage_end: Mapped[str | None] = mapped_column(String(10))
    coverage_summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    failure_json: Mapped[str | None] = mapped_column(Text)
    quality_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("quality_runs.run_id", ondelete="RESTRICT"), nullable=True
    )
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DatasetVersionBatchRow(Base):
    __tablename__ = "dataset_version_batches"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_dataset_version_batches_ordinal"),
        UniqueConstraint("version_id", "ordinal", name="uq_dataset_version_batches_ordinal"),
        Index("ix_dataset_version_batches_batch", "batch_id"),
    )

    version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.version_id", ondelete="CASCADE"), primary_key=True
    )
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("ingestion_batches.batch_id", ondelete="RESTRICT"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class QualityRunRow(Base):
    __tablename__ = "quality_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING','PASSED','FAILED','SKIPPED')",
            name="ck_quality_runs_status",
        ),
        Index("ix_quality_runs_version", "version_id"),
    )

    run_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.version_id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[str] = mapped_column(String(40), nullable=False)
    ended_at: Mapped[str | None] = mapped_column(String(40))
    results_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_report_relative_path: Mapped[str | None] = mapped_column(Text)
    failure_json: Mapped[str | None] = mapped_column(Text)


class QualityResultRow(Base):
    __tablename__ = "quality_results"
    __table_args__ = (
        CheckConstraint("sequence_number >= 0", name="ck_quality_results_sequence"),
        CheckConstraint("passed IN (0,1)", name="ck_quality_results_passed"),
        CheckConstraint("affected_count >= 0", name="ck_quality_results_affected_count"),
        UniqueConstraint("run_id", "rule_code", name="uq_quality_results_rule_code"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("quality_runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    sequence_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_code: Mapped[str] = mapped_column(String(128), nullable=False)
    passed: Mapped[int] = mapped_column(Integer, nullable=False)
    affected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    report_relative_path: Mapped[str | None] = mapped_column(Text)


class DifferenceReportRow(Base):
    __tablename__ = "difference_reports"
    __table_args__ = (
        Index("ix_difference_reports_version", "version_id"),
        Index("ix_difference_reports_primary_batch", "primary_batch_id"),
        Index("ix_difference_reports_fallback_batch", "fallback_batch_id"),
    )

    report_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.version_id", ondelete="RESTRICT"), nullable=False
    )
    primary_source: Mapped[str] = mapped_column(String(32), nullable=False)
    fallback_source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    field_differences_json: Mapped[str] = mapped_column(Text, nullable=False)
    total_compared: Mapped[int] = mapped_column(Integer, nullable=False)
    total_differences: Mapped[int] = mapped_column(Integer, nullable=False)
    report_relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    primary_batch_id: Mapped[str] = mapped_column(
        ForeignKey("ingestion_batches.batch_id", ondelete="RESTRICT"), nullable=False
    )
    fallback_batch_id: Mapped[str] = mapped_column(
        ForeignKey("ingestion_batches.batch_id", ondelete="RESTRICT"), nullable=False
    )
    primary_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.version_id", ondelete="RESTRICT"), nullable=False
    )
    fallback_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.version_id", ondelete="RESTRICT"), nullable=False
    )
    primary_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    fallback_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    primary_quality_run_id: Mapped[str] = mapped_column(
        ForeignKey("quality_runs.run_id", ondelete="RESTRICT"), nullable=False
    )
    fallback_quality_run_id: Mapped[str] = mapped_column(
        ForeignKey("quality_runs.run_id", ondelete="RESTRICT"), nullable=False
    )
    sample_spec_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    threshold_rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class ResolutionRunRow(Base):
    __tablename__ = "resolution_runs"
    __table_args__ = (
        CheckConstraint(
            "dataset = 'daily_bar_unadjusted'",
            name="ck_resolution_runs_daily_only",
        ),
        CheckConstraint(
            "length(input_fingerprint) = 64 AND length(manifest_sha256) = 64",
            name="ck_resolution_runs_sha_lengths",
        ),
        CheckConstraint(
            "total_decisions >= 0 AND default_decision_count >= 0 "
            "AND exception_count >= 0 AND conflict_count >= 0 AND blocker_count >= 0 "
            "AND total_decisions = default_decision_count + exception_count + conflict_count",
            name="ck_resolution_runs_counts",
        ),
        CheckConstraint(
            "(conflict_count = 0 AND blocker_count = 0 "
            "AND candidate_version_id IS NOT NULL) "
            "OR ((conflict_count > 0 OR blocker_count > 0) "
            "AND candidate_version_id IS NULL)",
            name="ck_resolution_runs_candidate",
        ),
        UniqueConstraint("created_job_id", name="uq_resolution_runs_job"),
        Index("ix_resolution_runs_created_at", "created_at", "run_id"),
        Index("ix_resolution_runs_fingerprint", "input_fingerprint"),
    )

    run_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    created_job_id: Mapped[str] = mapped_column(
        ForeignKey("job_runs.job_id", ondelete="RESTRICT"), nullable=False
    )
    candidate_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("dataset_versions.version_id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
    threshold_rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    total_decisions: Mapped[int] = mapped_column(Integer, nullable=False)
    default_decision_count: Mapped[int] = mapped_column(Integer, nullable=False)
    exception_count: Mapped[int] = mapped_column(Integer, nullable=False)
    conflict_count: Mapped[int] = mapped_column(Integer, nullable=False)
    blocker_count: Mapped[int] = mapped_column(Integer, nullable=False)


class SourceOverrideRow(Base):
    __tablename__ = "source_overrides"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','APPLIED')",
            name="ck_source_overrides_status",
        ),
        Index("ix_source_overrides_dataset_status", "dataset", "status"),
    )

    override_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    difference_report_id: Mapped[str] = mapped_column(
        ForeignKey("difference_reports.report_id", ondelete="RESTRICT"), nullable=False
    )
    primary_batch_id: Mapped[str] = mapped_column(
        ForeignKey("ingestion_batches.batch_id", ondelete="RESTRICT"), nullable=False
    )
    fallback_batch_id: Mapped[str] = mapped_column(
        ForeignKey("ingestion_batches.batch_id", ondelete="RESTRICT"), nullable=False
    )
    primary_source: Mapped[str] = mapped_column(String(32), nullable=False)
    fallback_source: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    scope_dates_json: Mapped[str] = mapped_column(Text, nullable=False)
    fields_json: Mapped[str] = mapped_column(Text, nullable=False)
    field_replacements_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    fallback_only_keys_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    exact_scope_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    contribution_scope_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    approved_at: Mapped[str | None] = mapped_column(String(40))
    approved_by: Mapped[str | None] = mapped_column(String(256))
    applied_at: Mapped[str | None] = mapped_column(String(40))
    applied_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("dataset_versions.version_id", ondelete="RESTRICT")
    )


class SourceOverrideApplicationRow(Base):
    __tablename__ = "source_override_applications"
    __table_args__ = (
        UniqueConstraint("child_version_id", name="uq_source_override_applications_child"),
        Index(
            "uq_source_override_applications_active_override",
            "override_id",
            unique=True,
            sqlite_where=text("status IN ('CLAIMED','PUBLISHED','APPLIED')"),
        ),
        CheckConstraint(
            "status IN ('CLAIMED','FAILED','PUBLISHED','APPLIED')",
            name="ck_source_override_applications_status",
        ),
    )

    application_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    override_id: Mapped[str] = mapped_column(
        ForeignKey("source_overrides.override_id", ondelete="RESTRICT"), nullable=False
    )
    child_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.version_id", ondelete="RESTRICT"), nullable=False
    )
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    report_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contribution_scope_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_by: Mapped[str] = mapped_column(String(256), nullable=False)
    claimed_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class SourceOverrideApplicationAttemptRow(Base):
    __tablename__ = "source_override_application_attempts"
    __table_args__ = (
        UniqueConstraint("override_id", "attempt_no", name="uq_override_attempt_no"),
        Index("ix_override_attempts_override_status", "override_id", "status"),
        CheckConstraint(
            "status IN ('CLAIMED','FAILED','PUBLISHED','APPLIED')",
            name="ck_source_override_application_attempts_status",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    override_id: Mapped[str] = mapped_column(
        ForeignKey("source_overrides.override_id", ondelete="RESTRICT"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    child_version_id: Mapped[str] = mapped_column(
        ForeignKey("dataset_versions.version_id", ondelete="RESTRICT"), nullable=False
    )
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    claimed_by: Mapped[str] = mapped_column(String(256), nullable=False)
    started_at: Mapped[str] = mapped_column(String(40), nullable=False)
    ended_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
