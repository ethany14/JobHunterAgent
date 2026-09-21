"""Safe public task API contracts."""
from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class TaskApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreatePlanRequest(TaskApiModel):
    template_id: str = Field(min_length=1, max_length=64)
    parent_session_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_session_version: int = Field(ge=0)


class TaskMutationRequest(TaskApiModel):
    expected_version: int = Field(ge=0)
    subtree: bool = False


class PublicTask(TaskApiModel):
    task_id: str
    workflow_mode: str = "single_custom"
    parent_task_id: str | None
    root_task_id: str
    task_type: str
    agent_role: str
    status: str
    priority: int
    depth: int
    version: int
    attempt_count: int
    max_attempts: int
    result_summary: dict | None
    output_artifacts: list[dict[str, str]] = Field(default_factory=list)
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    dependencies: list[dict[str, str]] = Field(default_factory=list)


class PublicTaskEvent(TaskApiModel):
    event_id: str
    sequence: int
    event_type: str
    attempt_id: str | None
    occurred_at: datetime


class PublicArtifactLink(TaskApiModel):
    artifact_id: str
    role: str
    direction: str
