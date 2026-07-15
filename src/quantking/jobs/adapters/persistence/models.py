"""SQLAlchemy rows, deliberately separate from jobs domain models."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Jobs persistence metadata."""


class JobRunRow(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        UniqueConstraint("job_type", "idempotency_key", name="uq_job_runs_type_idempotency"),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED','INTERRUPTED')",
            name="ck_job_runs_status",
        ),
        CheckConstraint("progress >= 0 AND progress <= 1", name="ck_job_runs_progress"),
        CheckConstraint("row_version >= 1", name="ck_job_runs_row_version"),
    )

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    input_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    config_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    version_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    requester: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(40))
    ended_at: Mapped[str | None] = mapped_column(String(40))
    heartbeat_at: Mapped[str | None] = mapped_column(String(40))
    stage: Mapped[str | None] = mapped_column(String(256))
    progress: Mapped[float] = mapped_column(nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(256))
    log_id: Mapped[str] = mapped_column(String(512), nullable=False)
    artifacts_json: Mapped[str] = mapped_column(Text, nullable=False)
    failure_json: Mapped[str | None] = mapped_column(Text)
    cancel_requested_at: Mapped[str | None] = mapped_column(String(40))
    cancel_request: Mapped[str | None] = mapped_column(Text)
    cancel_result: Mapped[str | None] = mapped_column(Text)
    non_cancellable_phase: Mapped[str | None] = mapped_column(String(256))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False)


class JobDependencyRow(Base):
    __tablename__ = "job_dependencies"

    job_id: Mapped[str] = mapped_column(
        ForeignKey("job_runs.job_id", ondelete="CASCADE"), primary_key=True
    )
    upstream_job_id: Mapped[str] = mapped_column(
        ForeignKey("job_runs.job_id", ondelete="RESTRICT"), primary_key=True
    )


class JobEventRow(Base):
    __tablename__ = "job_events"
    __table_args__ = (
        CheckConstraint(
            "progress IS NULL OR (progress >= 0 AND progress <= 1)",
            name="ck_events_progress",
        ),
        Index("ix_job_events_job_event", "job_id", "event_id"),
        {"sqlite_autoincrement": True},
    )

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job_runs.job_id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(256))
    progress: Mapped[float | None]
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False)


class AuditRecordRow(Base):
    __tablename__ = "audit_records"
    __table_args__ = (Index("ix_audit_records_action_id", "action", "audit_id"),)

    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    entity: Mapped[str] = mapped_column(String(256), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False)
