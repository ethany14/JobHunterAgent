"""SQLAlchemy persistence models for auditable tool calls."""
from datetime import datetime
from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from api.db import Base

class ToolCallRow(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", "tool_name", "idempotency_key", name="uq_tool_call_idempotency"),
        CheckConstraint("status IN ('requested','approval_required','approved','running','completed','failed','denied','timed_out','outcome_unknown')", name="ck_tool_calls_status"),
    )
    call_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_json: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_side_effect: Mapped[str | None] = mapped_column(String(32))
    tool_idempotent: Mapped[bool | None] = mapped_column(Boolean)
    execution_attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    execution_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    timeout_seconds: Mapped[float | None] = mapped_column(Float)
    approval_tool_name: Mapped[str | None] = mapped_column(String(128))
    approval_tool_version: Mapped[str | None] = mapped_column(String(64))
    approval_arguments_hash: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

class ToolCallEventRow(Base):
    __tablename__ = "tool_call_events"
    __table_args__ = (UniqueConstraint("call_id", "sequence", name="uq_tool_call_event_sequence"),)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("tool_calls.call_id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
