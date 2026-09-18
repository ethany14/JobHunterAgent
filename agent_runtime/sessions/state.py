"""Validated state and message contracts for persisted sessions."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from agent_runtime.tools.messages import AgentMessage
from agent_runtime.types import RuntimeModel


class SessionStatus(StrEnum):
    ACTIVE = "active"
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"
    AWAITING_TOOL_APPROVAL = "awaiting_tool_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_SESSION_STATUSES = frozenset(
    {
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
        SessionStatus.TIMED_OUT,
    }
)


class SessionMessageVisibility(StrEnum):
    SESSION = "session"
    SHARED = "shared"
    TASK_PRIVATE = "task_private"


class SessionState(RuntimeModel):
    schema_version: int = Field(default=3, ge=1)
    session_id: str = Field(min_length=1, max_length=128)
    user_id: str | None = Field(default=None, max_length=128)
    title: str | None = Field(default=None, max_length=256)
    status: SessionStatus = SessionStatus.ACTIVE
    version: int = Field(default=0, ge=0)
    event_sequence: int = Field(default=0, ge=0)
    message_sequence: int = Field(default=0, ge=0)
    active_run_id: str | None = Field(default=None, max_length=128)
    pending_assistant_message_id: str | None = Field(default=None, max_length=128)
    pending_tool_call_ids: list[str] = Field(default_factory=list)
    allowed_tools: frozenset[str] = Field(default_factory=frozenset)
    loop_iteration: int = Field(default=0, ge=0)
    executed_tool_calls: int = Field(default=0, ge=0)
    max_loop_iterations: int = Field(default=6, ge=1)
    max_tool_calls: int = Field(default=10, ge=1)
    total_input_tokens: int = Field(default=0, ge=0)
    total_output_tokens: int = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = None
    cancel_requested: bool = False
    cancel_requested_at: datetime | None = None
    cancel_reason: str | None = Field(default=None, max_length=512)
    turn_deadline_at: datetime | None = None
    session_expires_at: datetime | None = None
    terminal_reason: str | None = Field(default=None, max_length=128)
    manual_recovery_tool_call_ids: list[str] = Field(default_factory=list)
    allowed_skills: frozenset[str] = Field(default_factory=frozenset)
    current_context_snapshot_id: str | None = Field(default=None, max_length=36)
    last_context_snapshot_id: str | None = Field(default=None, max_length=36)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def upgrade_legacy_state(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "schema_version" not in value:
            return value
        upgraded = dict(value)
        schema_version = int(upgraded.get("schema_version", 1))
        if schema_version < 3:
            upgraded.setdefault("allowed_skills", [])
            upgraded.setdefault("current_context_snapshot_id", None)
            upgraded.setdefault("last_context_snapshot_id", None)
            upgraded["schema_version"] = 3
        if schema_version >= 2:
            return upgraded
        status = upgraded.get("status")
        status_value = status.value if isinstance(status, SessionStatus) else status
        if status_value in {"completed", "failed", "cancelled"}:
            upgraded.setdefault("terminal_reason", f"legacy_{status_value}")
        if status_value == "cancelled":
            upgraded.setdefault("cancel_requested", True)
            upgraded.setdefault(
                "cancel_requested_at",
                upgraded.get("updated_at")
                or upgraded.get("created_at")
                or datetime.now(UTC),
            )
            upgraded.setdefault("cancel_reason", "Legacy cancellation")
        return upgraded

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "SessionState":
        if len(self.pending_tool_call_ids) != len(set(self.pending_tool_call_ids)):
            raise ValueError("Pending tool-call IDs must be unique.")
        if self.pending_tool_call_ids and self.status not in {
            SessionStatus.RUNNING,
            SessionStatus.AWAITING_TOOL_APPROVAL,
        }:
            raise ValueError(
                "Pending tool calls are valid only while running or awaiting approval."
            )
        if (
            self.status == SessionStatus.AWAITING_TOOL_APPROVAL
            and not self.pending_tool_call_ids
        ):
            raise ValueError("A session awaiting tool approval requires pending calls.")
        if self.pending_tool_call_ids and not self.pending_assistant_message_id:
            raise ValueError("Pending tool calls require their assistant message ID.")
        if self.pending_assistant_message_id and not self.pending_tool_call_ids:
            raise ValueError("A pending assistant message requires pending tool calls.")
        if self.status == SessionStatus.FAILED and not self.error_code:
            raise ValueError("A failed session requires a safe error code.")
        if self.error_message and not self.error_code:
            raise ValueError("A safe error message requires an error code.")
        if self.cancel_requested:
            if self.cancel_requested_at is None or not self.cancel_reason:
                raise ValueError(
                    "A cancellation request requires its timestamp and reason."
                )
        elif self.cancel_requested_at is not None or self.cancel_reason is not None:
            raise ValueError(
                "Cancellation metadata is invalid without a cancellation request."
            )
        if len(self.manual_recovery_tool_call_ids) != len(
            set(self.manual_recovery_tool_call_ids)
        ):
            raise ValueError("Manual-recovery tool-call IDs must be unique.")
        if self.manual_recovery_tool_call_ids and self.status != SessionStatus.AWAITING_USER:
            raise ValueError("Manual recovery requires an awaiting-user session.")
        if self.status in TERMINAL_SESSION_STATUSES:
            if not self.terminal_reason:
                raise ValueError("A terminal session requires a terminal reason.")
            if self.pending_tool_call_ids or self.pending_assistant_message_id:
                raise ValueError("A terminal session cannot retain pending tool calls.")
        elif self.terminal_reason is not None:
            raise ValueError("Only a terminal session may have a terminal reason.")
        if self.status == SessionStatus.CANCELLED and not self.cancel_requested:
            raise ValueError("A cancelled session requires a cancellation request.")
        return self

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_SESSION_STATUSES


class SessionMessageDraft(RuntimeModel):
    message: AgentMessage
    task_id: str | None = Field(default=None, max_length=128)
    visibility: SessionMessageVisibility = SessionMessageVisibility.SESSION

    @model_validator(mode="after")
    def validate_visibility(self) -> "SessionMessageDraft":
        if self.visibility == SessionMessageVisibility.TASK_PRIVATE and not self.task_id:
            raise ValueError("Task-private messages require a task ID.")
        return self


class PersistedSessionMessage(RuntimeModel):
    message_id: str
    session_id: str
    sequence: int = Field(ge=1)
    task_id: str | None = None
    visibility: SessionMessageVisibility
    message: AgentMessage
    content_hash: str = Field(min_length=64, max_length=64)
    created_at: datetime
