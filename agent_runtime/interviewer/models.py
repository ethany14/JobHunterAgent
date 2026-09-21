"""SQLAlchemy rows for application-scoped interviews."""
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class ApplicationRequirementAssessmentRow(Base):
    __tablename__ = "application_requirement_assessments"
    __table_args__ = (
        UniqueConstraint("application_id", "snapshot_id", "source_match_artifact_id",
                         "requirement_id", name="uq_app_requirement_match"),
    )
    assessment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.application_id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("job_snapshots.snapshot_id"), nullable=False, index=True)
    requirement_id: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_requirement: Mapped[str] = mapped_column(String(256), nullable=False)
    original_requirement_text: Mapped[str] = mapped_column(Text, nullable=False)
    requirement_level: Mapped[str] = mapped_column(String(16), nullable=False)
    match_status: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_status: Mapped[str] = mapped_column(String(32), nullable=False)
    linked_evidence_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_match_artifact_id: Mapped[str] = mapped_column(ForeignKey("application_artifacts.artifact_id"), nullable=False)
    jd_order: Mapped[int] = mapped_column(Integer, nullable=False)
    interview_exhausted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InterviewSessionRow(Base):
    __tablename__ = "interview_sessions"
    __table_args__ = (
        CheckConstraint("status IN ('planning','awaiting_answer','awaiting_evidence_confirmation','completed','cancelled','failed')", name="ck_interview_status"),
    )
    interview_session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.application_id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("job_snapshots.snapshot_id"), nullable=False)
    source_match_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("application_artifacts.artifact_id"))
    agent_session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.session_id"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    current_assessment_id: Mapped[str | None] = mapped_column(ForeignKey("application_requirement_assessments.assessment_id"))
    pending_answer_turn_id: Mapped[str | None] = mapped_column(String(36))
    pending_candidate_evidence_id: Mapped[str | None] = mapped_column(ForeignKey("career_evidence.evidence_id"))
    questions_asked: Mapped[int] = mapped_column(Integer, nullable=False)
    max_questions: Mapped[int] = mapped_column(Integer, nullable=False)
    followups_for_current_requirement: Mapped[int] = mapped_column(Integer, nullable=False)
    max_followups_per_requirement: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    turn_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    last_action_key: Mapped[str | None] = mapped_column(String(128))
    last_action_kind: Mapped[str | None] = mapped_column(String(40))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InterviewTurnRow(Base):
    __tablename__ = "interview_turns"
    __table_args__ = (
        UniqueConstraint("interview_session_id", "sequence", name="uq_interview_turn_sequence"),
        UniqueConstraint("interview_session_id", "idempotency_key", name="uq_interview_turn_idempotency"),
    )
    turn_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    interview_session_id: Mapped[str] = mapped_column(ForeignKey("interview_sessions.interview_session_id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    assessment_id: Mapped[str | None] = mapped_column(ForeignKey("application_requirement_assessments.assessment_id"))
    turn_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
