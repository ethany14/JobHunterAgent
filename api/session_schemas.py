"""Safe HTTP contracts for persistent agent sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.sessions.outcome import SessionOutcomeStatus
from agent_runtime.sessions.state import SessionStatus
from agent_runtime.types import ToolDataClassification, ToolSideEffect


class SessionApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


CapabilityProfile = Literal["job_assistant_readonly"]


class CreateSessionRequest(SessionApiModel):
    capability_profile: CapabilityProfile = "job_assistant_readonly"
    title: str | None = Field(default=None, max_length=256)
    active_run_id: str | None = Field(default=None, min_length=1, max_length=128)


class SubmitMessageRequest(SessionApiModel):
    message_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=50_000)
    expected_version: int = Field(ge=0)


class VersionedMutationRequest(SessionApiModel):
    expected_version: int = Field(ge=0)


class CancelSessionRequest(VersionedMutationRequest):
    reason: str = Field(min_length=1, max_length=512)


class PublicSession(SessionApiModel):
    session_id: str
    title: str | None
    capability_profile: CapabilityProfile
    status: SessionStatus
    version: int
    active_run_id: str | None
    loop_iteration: int
    executed_tool_calls: int
    total_input_tokens: int
    total_output_tokens: int
    error_code: str | None
    error_message: str | None
    cancel_requested: bool
    turn_deadline_at: datetime | None
    session_expires_at: datetime | None
    terminal_reason: str | None
    manual_recovery_tool_call_ids: list[str]
    recovery_available: bool
    created_at: datetime
    updated_at: datetime


class PublicSessionMessage(SessionApiModel):
    message_id: str
    sequence: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class PendingToolApproval(SessionApiModel):
    call_id: str
    tool_name: str
    tool_version: str | None
    description: str
    side_effect: ToolSideEffect
    data_classification: ToolDataClassification
    arguments: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class PublicToolCall(SessionApiModel):
    call_id: str
    display_name: str
    public_tool_name: str
    provider: Literal["Built-in", "MCP"]
    mcp_server_id: str | None = None
    remote_tool_name: str | None = None
    status: str
    side_effect: ToolSideEffect | None = None
    duration_ms: int | None = None
    approval_status: Literal["not_required", "required", "approved", "rejected"]
    result_truncated: bool = False
    idempotently_reused: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None


class SessionResponse(SessionApiModel):
    session: PublicSession
    outcome_status: SessionOutcomeStatus | None = None
    response: str | None = None
    pending_tool_approvals: list[PendingToolApproval] = Field(default_factory=list)
    tool_calls: list[PublicToolCall] = Field(default_factory=list)


class SessionMessagesResponse(SessionApiModel):
    session_id: str
    messages: list[PublicSessionMessage]


class SessionSummary(SessionApiModel):
    session_id: str
    title: str | None
    status: SessionStatus
    active_run_id: str | None
    message_count: int
    created_at: datetime
    updated_at: datetime


class SessionListResponse(SessionApiModel):
    sessions: list[SessionSummary]


class ApiErrorDetail(SessionApiModel):
    code: str
    message: str


class ApiErrorResponse(SessionApiModel):
    detail: ApiErrorDetail
