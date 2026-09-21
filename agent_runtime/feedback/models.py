"""Alembic-managed feedback and learning projections."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class FeedbackEventRow(Base):
    __tablename__ = "feedback_events"
    __table_args__ = (UniqueConstraint("owner_id", "source_action_id", name="uq_feedback_source_action"),)
    feedback_event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    application_id: Mapped[str | None] = mapped_column(String(36), index=True)
    session_id: Mapped[str | None] = mapped_column(String(128))
    task_id: Mapped[str | None] = mapped_column(String(36))
    artifact_id: Mapped[str | None] = mapped_column(String(36))
    artifact_version: Mapped[int | None] = mapped_column(Integer)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_action_id: Mapped[str] = mapped_column(String(160), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_strength: Mapped[str] = mapped_column(String(16), nullable=False)
    processed_status: Mapped[str] = mapped_column(String(40), nullable=False)
    processing_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LearningCandidateRow(Base):
    __tablename__ = "learning_candidates"
    candidate_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    candidate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    canonical_key: Mapped[str] = mapped_column(String(160), nullable=False)
    scope: Mapped[str] = mapped_column(String(24), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Index("ix_learning_aggregate", LearningCandidateRow.owner_id,
      LearningCandidateRow.candidate_type, LearningCandidateRow.scope,
      LearningCandidateRow.scope_id, LearningCandidateRow.canonical_key)


class LearningCandidateEventLinkRow(Base):
    __tablename__ = "learning_candidate_event_links"
    candidate_id: Mapped[str] = mapped_column(ForeignKey("learning_candidates.candidate_id"), primary_key=True)
    feedback_event_id: Mapped[str] = mapped_column(ForeignKey("feedback_events.feedback_event_id"), primary_key=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LearningCandidateConflictRow(Base):
    __tablename__ = "learning_candidate_conflicts"
    candidate_id: Mapped[str] = mapped_column(ForeignKey("learning_candidates.candidate_id"), primary_key=True)
    conflicting_candidate_id: Mapped[str] = mapped_column(ForeignKey("learning_candidates.candidate_id"), primary_key=True)
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FeedbackProcessingAttemptRow(Base):
    __tablename__ = "feedback_processing_attempts"
    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    feedback_event_id: Mapped[str] = mapped_column(ForeignKey("feedback_events.feedback_event_id"), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    classification_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("feedback_event_id", "attempt_number", name="uq_feedback_attempt_number"),)
