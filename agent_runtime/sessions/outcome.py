"""Public outcomes returned by resumable session coordination."""
from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from agent_runtime.sessions.state import SessionState
from agent_runtime.types import RuntimeModel


class SessionOutcomeStatus(StrEnum):
    RESPONSE_READY = "response_ready"
    AWAITING_TOOL_APPROVAL = "awaiting_tool_approval"
    AWAITING_USER = "awaiting_user"
    FAILED = "failed"
    LIMIT_EXCEEDED = "limit_exceeded"


class SessionOutcome(RuntimeModel):
    status: SessionOutcomeStatus
    session_id: str
    state: SessionState
    final_text: str | None = None
    pending_tool_call_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
