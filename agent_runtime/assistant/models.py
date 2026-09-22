"""Alembic-managed Assistant timeline and action request projections."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class AssistantActivityRow(Base):
    __tablename__ = "assistant_activities"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_assistant_activity_sequence"),
        UniqueConstraint("session_id", "reference_type", "reference_id",
                         name="uq_assistant_activity_reference"),
    )
    activity_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.session_id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    activity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reference_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AssistantActionRow(Base):
    __tablename__ = "assistant_action_requests"
    __table_args__ = (
        UniqueConstraint("session_id", "idempotency_key", name="uq_assistant_action_key"),
    )
    action_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.session_id", ondelete="CASCADE"), index=True)
    action_type: Mapped[str] = mapped_column(String(48), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(64))
    reference_id: Mapped[str | None] = mapped_column(String(128))
    safe_result_json: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
