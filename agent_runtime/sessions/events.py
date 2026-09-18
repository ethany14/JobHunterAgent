"""Append-only audit events for session lifecycle changes."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import Field

from agent_runtime.types import RuntimeModel


class SessionEventType(StrEnum):
    SESSION_CREATED = "session_created"
    MESSAGES_APPENDED = "messages_appended"
    STATUS_CHANGED = "status_changed"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_TOOL_APPROVAL = "waiting_for_tool_approval"
    SESSION_RESUMED = "session_resumed"
    SESSION_COMPLETED = "session_completed"
    SESSION_FAILED = "session_failed"
    SESSION_CANCELLED = "session_cancelled"
    MODEL_DECISION_PERSISTED = "model_decision_persisted"
    TOOL_RESULT_PERSISTED = "tool_result_persisted"
    TOOL_APPROVED = "tool_approved"
    TOOL_REJECTED = "tool_rejected"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_ACTION_PERSISTED = "recovery_action_persisted"
    CANCEL_REQUESTED = "cancel_requested"
    SESSION_TIMED_OUT = "session_timed_out"
    MODEL_RESULT_DISCARDED = "model_result_discarded"
    TOOL_RESULT_DISCARDED = "tool_result_discarded"
    MANUAL_RECOVERY_REQUIRED = "manual_recovery_required"
    CONTEXT_SNAPSHOT_PREPARED = "context_snapshot_prepared"
    CONTEXT_SNAPSHOT_USED = "context_snapshot_used"
    CONTEXT_SNAPSHOT_ABANDONED = "context_snapshot_abandoned"


class SessionEvent(RuntimeModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    # Repository-owned. Zero is accepted only for a not-yet-persisted event.
    sequence: int = Field(default=0, ge=0)
    event_type: SessionEventType
    payload: dict = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
