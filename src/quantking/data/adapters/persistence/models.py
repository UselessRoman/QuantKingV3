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

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Stage 1 data metadata persistence metadata, separate from the jobs Base."""


class IngestionBatchRow(Base):
    __tablename__ = "ingestion_batches"
    __table_args__ = (
        UniqueConstraint(
            "source", "dataset", "request_params_sha256", name="uq_batches_source_dataset_params"
        ),
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
    parent_version_id: Mapped[str | None] = mapped_column(String(96))
    input_batch_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    validated_at: Mapped[str | None] = mapped_column(String(40))
    published_at: Mapped[str | None] = mapped_column(String(40))
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    security_count: Mapped[int | None] = mapped_column(Integer)
    row_count: Mapped[int | None] = mapped_column(Integer)
    failure_json: Mapped[str | None] = mapped_column(Text)


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
    started_at: Mapped[str] = mapped_column(String(40), nullable=False)
    ended_at: Mapped[str | None] = mapped_column(String(40))
    results_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_report_relative_path: Mapped[str | None] = mapped_column(Text)
    failure_json: Mapped[str | None] = mapped_column(Text)


class DifferenceReportRow(Base):
    __tablename__ = "difference_reports"
    __table_args__ = (Index("ix_difference_reports_version", "version_id"),)

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
    primary_source: Mapped[str] = mapped_column(String(32), nullable=False)
    fallback_source: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_codes_json: Mapped[str] = mapped_column(Text, nullable=False)
    scope_dates_json: Mapped[str] = mapped_column(Text, nullable=False)
    fields_json: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    approved_at: Mapped[str | None] = mapped_column(String(40))
    approved_by: Mapped[str | None] = mapped_column(String(256))
    applied_at: Mapped[str | None] = mapped_column(String(40))
    applied_version_id: Mapped[str | None] = mapped_column(String(96))
