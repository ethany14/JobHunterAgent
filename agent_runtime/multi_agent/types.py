"""Public contracts for deterministic task plans and isolated workers."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import Field, model_validator

from agent_runtime.types import RuntimeModel


class TaskStatus(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    READY = "ready"
    CLAIMED = "claimed"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_INPUT = "awaiting_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL = frozenset({TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT})


class DependencyType(StrEnum):
    REQUIRES_SUCCESS = "requires_success"
    REQUIRES_COMPLETION = "requires_completion"


class ArtifactDirection(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class TaskEventType(StrEnum):
    TASK_CREATED = "task_created"
    DEPENDENCY_ADDED = "dependency_added"
    TASK_READY = "task_ready"
    TASK_CLAIMED = "task_claimed"
    TASK_STARTED = "task_started"
    CHILD_SESSION_CREATED = "child_session_created"
    CONTEXT_ASSEMBLED = "context_assembled"
    TOOL_APPROVAL_REQUIRED = "tool_approval_required"
    TASK_PAUSED = "task_paused"
    TASK_RESUMED = "task_resumed"
    ARTIFACT_CREATED = "artifact_created"
    TASK_SUCCEEDED = "task_succeeded"
    TASK_FAILED = "task_failed"
    RETRY_SCHEDULED = "retry_scheduled"
    CANCEL_REQUESTED = "cancel_requested"
    TASK_CANCELLED = "task_cancelled"
    TASK_TIMED_OUT = "task_timed_out"
    LEASE_EXPIRED = "lease_expired"
    TASK_RECOVERED = "task_recovered"


class MultiAgentBudget(RuntimeModel):
    max_tasks: int = Field(default=20, ge=1, le=100)
    max_depth: int = Field(default=3, ge=0, le=24)
    max_children_per_task: int = Field(default=10, ge=0, le=100)
    max_parallel_tasks: int = Field(default=2, ge=1, le=16)
    max_model_calls: int = Field(default=20, ge=0)
    max_tool_calls: int = Field(default=40, ge=0)
    max_input_tokens: int = Field(default=100_000, ge=0)
    max_output_tokens: int = Field(default=30_000, ge=0)
    max_estimated_cost: float | None = Field(default=None, ge=0)
    deadline_at: datetime | None = None


class TaskSpec(RuntimeModel):
    key: str = Field(min_length=1, max_length=64)
    task_type: str = Field(min_length=1, max_length=64)
    agent_role: str = Field(min_length=1, max_length=64)
    parent_key: str | None = None
    application_id: str | None = None
    priority: int = 0
    max_attempts: int = Field(default=1, ge=1, le=25)
    input_spec: dict[str, Any] = Field(default_factory=dict)
    output_spec: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: frozenset[str] = Field(default_factory=frozenset)
    allowed_skill_version_ids: frozenset[str] = Field(default_factory=frozenset)
    input_artifact_ids: list[str] = Field(default_factory=list)
    input_from_tasks: dict[str, str] = Field(default_factory=dict)
    optional_input_from_tasks: dict[str, str] = Field(default_factory=dict)
    recent_parent_message_limit: int = Field(default=0, ge=0, le=20)
    token_budget: int = Field(default=4000, ge=1)
    deadline_at: datetime | None = None


class TaskDependencySpec(RuntimeModel):
    task_key: str
    depends_on_key: str
    dependency_type: DependencyType = DependencyType.REQUIRES_SUCCESS


class AgentPlan(RuntimeModel):
    template_id: str
    workflow_mode: str = "single_custom"
    parent_session_id: str
    root_key: str
    tasks: list[TaskSpec]
    dependencies: list[TaskDependencySpec] = Field(default_factory=list)
    budget: MultiAgentBudget = Field(default_factory=MultiAgentBudget)
    idempotency_key: str = Field(min_length=1, max_length=128)


class AgentTask(RuntimeModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    task_type: str
    agent_role: str
    parent_task_id: str | None = None
    root_task_id: str
    parent_session_id: str
    child_session_id: str | None = None
    application_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 0
    depth: int = Field(ge=0)
    version: int = Field(default=0, ge=0)
    event_sequence: int = Field(default=0, ge=0)
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    active_attempt_id: str | None = None
    lease_until: datetime | None = None
    deadline_at: datetime | None = None
    idempotency_key: str
    input_spec: dict[str, Any] = Field(default_factory=dict)
    output_spec: dict[str, Any] = Field(default_factory=dict)
    result_summary: dict[str, Any] | None = None
    error_code: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def valid_attempt(self) -> "AgentTask":
        if self.status in {TaskStatus.CLAIMED, TaskStatus.RUNNING} and not self.active_attempt_id:
            raise ValueError("Claimed/running tasks require an active attempt.")
        if self.status in TERMINAL and self.active_attempt_id:
            raise ValueError("Terminal tasks cannot retain an active attempt.")
        return self


class AgentTaskEvent(RuntimeModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    sequence: int = Field(ge=1)
    event_type: TaskEventType
    attempt_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OutputArtifact(RuntimeModel):
    role: str = Field(min_length=1, max_length=64)
    content: dict[str, Any]


class AgentTaskResult(RuntimeModel):
    summary: str = Field(max_length=2000)
    output_artifacts: list[OutputArtifact] = Field(default_factory=list)
    usage: dict[str, int | float] = Field(default_factory=dict)
    next_action_suggestions: list[str] = Field(default_factory=list)
    awaiting_approval: bool = False
    awaiting_input: bool = False
    pause_metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_pause_metadata(self) -> "AgentTaskResult":
        if len(self.pause_metadata) > 8 or any(
            len(key) > 64 or len(value) > 128 or "\n" in value
            for key, value in self.pause_metadata.items()
        ):
            raise ValueError("Pause metadata must contain only bounded identifiers.")
        return self


class ExecutionContext(RuntimeModel):
    task_id: str
    attempt_id: str
    child_session_id: str
    context_snapshot_id: str
    context_hash: str
    allowed_tools: frozenset[str]
    allowed_skill_version_ids: frozenset[str]
    input_artifacts: dict[str, dict[str, Any]]
    parent_messages: tuple[dict[str, str], ...] = ()
    memory_items: tuple[dict[str, Any], ...] = ()
    evidence_items: tuple[dict[str, Any], ...] = ()
    skill_procedures: tuple[dict[str, str], ...] = ()
    memory_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    parent_message_ids: tuple[str, ...] = ()
    deadline_at: datetime | None = None
    usage_budget: MultiAgentBudget
    tracing_metadata: dict[str, str] = Field(default_factory=dict)

    def tool_context(self):
        """A worker cannot add tool permissions when constructing ToolContext."""
        from agent_runtime.types import ToolContext
        return ToolContext(task_id=self.task_id, session_id=self.child_session_id,
            attempt_id=self.attempt_id, agent_name="child_task",
            allowed_tools=self.allowed_tools, turn_deadline_at=self.deadline_at)


class AgentWorker(Protocol):
    task_type: str
    def execute(self, task: AgentTask, context: ExecutionContext) -> AgentTaskResult: ...
