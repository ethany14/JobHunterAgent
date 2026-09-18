"""Validated contracts shared by the tool runtime."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field

class RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class ToolRiskLevel(StrEnum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    SENSITIVE = "sensitive"

class ToolSideEffect(StrEnum):
    NONE = "none"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"

class ToolDataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"

class ToolExecutionStatus(StrEnum):
    REQUESTED = "requested"
    APPROVAL_REQUIRED = "approval_required"
    APPROVED = "approved"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    OUTCOME_UNKNOWN = "outcome_unknown"

class ToolProvenance(RuntimeModel):
    source_type: str = Field(min_length=1)
    source_id: str | None = None
    source_uri: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

class ToolContext(RuntimeModel):
    run_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    user_id: str | None = None
    agent_name: str | None = None
    attempt_id: str | None = None
    lease_until: datetime | None = None
    turn_deadline_at: datetime | None = None
    remaining_timeout_seconds: float | None = Field(default=None, gt=0)
    allowed_tools: frozenset[str] = Field(default_factory=frozenset)

class ToolResult(RuntimeModel):
    output: Any = None
    provenance: list[ToolProvenance] = Field(default_factory=list)
    is_error: bool = False
    error_code: str | None = None

class ToolCallRequest(RuntimeModel):
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    retry_failed: bool = False
    max_attempts: int = Field(default=1, ge=1)
    # Kept for request compatibility; it is never sufficient for a write.
    approval_granted: bool = False

class ToolCallRecord(RuntimeModel):
    call_id: str = Field(default_factory=lambda: str(uuid4()))
    request: ToolCallRequest
    status: ToolExecutionStatus
    tool_version: str | None = None
    risk_level: ToolRiskLevel | None = None
    side_effect: ToolSideEffect | None = None
    idempotent: bool | None = None
    execution_attempt_id: str | None = None
    execution_lease_until: datetime | None = None
    scope_type: str | None = None
    scope_id: str | None = None
    arguments_hash: str | None = None
    result: ToolResult | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    attempt_count: int = Field(default=0, ge=0)
    version: int = Field(default=0, ge=0)
    event_sequence: int = Field(default=0, ge=0)
    approval_tool_name: str | None = None
    approval_tool_version: str | None = None
    approval_arguments_hash: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

class ToolCallEventRecord(RuntimeModel):
    event_id: str
    call_id: str
    sequence: int
    event_type: str
    from_status: ToolExecutionStatus | None = None
    to_status: ToolExecutionStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime

@runtime_checkable
class AgentTool(Protocol):
    name: str
    version: str
    description: str
    risk_level: ToolRiskLevel
    side_effect: ToolSideEffect
    data_classification: ToolDataClassification
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    timeout_seconds: float | None
    idempotent: bool
    def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult: ...

RESUME_EVIDENCE_SOURCE_TYPES = frozenset({"resume_source", "user_confirmed"})

def provenance_may_support_resume_claim(provenance: ToolProvenance) -> bool:
    return provenance.source_type in RESUME_EVIDENCE_SOURCE_TYPES
