"""Alembic-managed conversation-learning persistence."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class ConversationExperienceRow(Base):
    __tablename__ = "conversation_experiences"
    __table_args__ = (
        UniqueConstraint("session_id", "user_message_id", "assistant_message_id",
                         name="uq_conversation_experience_turn"),
    )
    experience_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.session_id", ondelete="CASCADE"), nullable=False, index=True)
    application_id: Mapped[str | None] = mapped_column(String(36), index=True)
    user_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    assistant_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationExperienceEventRow(Base):
    __tablename__ = "conversation_experience_events"
    __table_args__ = (
        UniqueConstraint("experience_id", "sequence", name="uq_conversation_experience_event"),
    )
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experience_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_experiences.experience_id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationLearningPatternRow(Base):
    __tablename__ = "conversation_learning_patterns"
    __table_args__ = (
        UniqueConstraint("owner_id", "canonical_key", name="uq_conversation_pattern_key"),
    )
    pattern_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    canonical_key: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    emitted_feedback_event_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationPatternExperienceRow(Base):
    __tablename__ = "conversation_pattern_experiences"
    pattern_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_learning_patterns.pattern_id", ondelete="CASCADE"),
        primary_key=True)
    experience_id: Mapped[str] = mapped_column(
        ForeignKey("conversation_experiences.experience_id", ondelete="CASCADE"),
        primary_key=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
