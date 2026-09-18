"""SQLAlchemy rows for session snapshots, messages, and events."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class AgentSessionRow(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','running','awaiting_user','awaiting_tool_approval',"
            "'completed','failed','cancelled','timed_out')",
            name="ck_agent_sessions_status",
        ),
    )

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(128), index=True)
    title: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    message_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    active_run_id: Mapped[str | None] = mapped_column(String(128), index=True)
    pending_assistant_message_id: Mapped[str | None] = mapped_column(String(128))
    pending_tool_call_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_tools_json: Mapped[str] = mapped_column(Text, nullable=False)
    loop_iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    executed_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    max_loop_iterations: Mapped[int] = mapped_column(Integer, nullable=False)
    max_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    total_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    total_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active_attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    cancel_requested: Mapped[bool] = mapped_column(nullable=False, default=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    turn_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    session_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    terminal_reason: Mapped[str | None] = mapped_column(String(128))
    manual_recovery_tool_call_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allowed_skills_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    current_context_snapshot_id: Mapped[str | None] = mapped_column(String(36), index=True)
    last_context_snapshot_id: Mapped[str | None] = mapped_column(String(36), index=True)


class AgentSessionMessageRow(Base):
    __tablename__ = "agent_session_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_session_message_sequence"),
        CheckConstraint(
            "visibility IN ('session','shared','task_private')",
            name="ck_session_message_visibility",
        ),
    )

    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("agent_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(128), index=True)
    visibility: Mapped[str] = mapped_column(String(24), nullable=False)
    message_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentSessionEventRow(Base):
    __tablename__ = "agent_session_events"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_session_event_sequence"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("agent_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentSessionAttemptRow(Base):
    __tablename__ = "agent_session_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','released','expired')",
            name="ck_session_attempt_status",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("agent_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovered_from_attempt_id: Mapped[str | None] = mapped_column(String(36))
