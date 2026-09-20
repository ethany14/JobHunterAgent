"""SQLAlchemy rows for Job Workspace."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class JobRow(Base):
    __tablename__ = "jobs"
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    canonical_url: Mapped[str | None] = mapped_column(String(2048), unique=True, index=True)
    source_site: Mapped[str | None] = mapped_column(String(128))
    company: Mapped[str | None] = mapped_column(String(256), index=True)
    title: Mapped[str | None] = mapped_column(String(256), index=True)
    location: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JobSnapshotRow(Base):
    __tablename__ = "job_snapshots"
    __table_args__ = (UniqueConstraint("job_id", "content_hash", name="uq_job_snapshot_content"),)
    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), nullable=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_page_text: Mapped[str | None] = mapped_column(Text)
    cleaned_job_description: Mapped[str] = mapped_column(Text, nullable=False)
    extraction_metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


_APPLICATION_STATUSES = "'saved','analyzing','analysis_failed','needs_evidence','materials_ready','ready_to_apply','applied','interviewing','offer','rejected','withdrawn','archived'"


class ApplicationRow(Base):
    __tablename__ = "applications"
    __table_args__ = (CheckConstraint(f"status IN ({_APPLICATION_STATUSES})", name="ck_applications_status"),)
    application_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), nullable=False, index=True)
    current_snapshot_id: Mapped[str] = mapped_column(ForeignKey("job_snapshots.snapshot_id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    next_action: Mapped[str | None] = mapped_column(Text)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApplicationArtifactRow(Base):
    __tablename__ = "application_artifacts"
    __table_args__ = (
        UniqueConstraint("application_id", "artifact_type", "version", name="uq_application_artifact_version"),
        UniqueConstraint("application_id", "artifact_type", "source_run_id", name="uq_application_artifact_source_run"),
        CheckConstraint("status IN ('draft','verified','approved','superseded')", name="ck_application_artifact_status"),
    )
    artifact_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.application_id", ondelete="CASCADE"), nullable=False, index=True)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    verification_status: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    source_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id"), index=True)
    source_session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_sessions.session_id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApplicationEventRow(Base):
    __tablename__ = "application_events"
    __table_args__ = (UniqueConstraint("application_id", "sequence", name="uq_application_event_sequence"),)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.application_id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApplicationRunRow(Base):
    __tablename__ = "application_runs"
    __table_args__ = (UniqueConstraint("application_id", "run_id", name="uq_application_run"),)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.application_id", ondelete="CASCADE"), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    attached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApplicationSessionRow(Base):
    __tablename__ = "application_sessions"
    __table_args__ = (UniqueConstraint("application_id", "session_id", name="uq_application_session"),)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.application_id", ondelete="CASCADE"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.session_id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    attached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
