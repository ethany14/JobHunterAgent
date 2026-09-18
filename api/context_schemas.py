"""Safe HTTP contracts for governed Memory, Skill, and context management."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_runtime.context.snapshots import ContextSnapshotStatus
from agent_runtime.memory.types import MemoryScope, MemorySensitivity, MemoryStatus, MemoryType
from agent_runtime.skills.types import SkillStatus


class ContextApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateMemoryRequest(ContextApiModel):
    scope: MemoryScope
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    memory_key: str = Field(min_length=1, max_length=160)
    memory_type: MemoryType
    display_text: str = Field(min_length=1, max_length=2_000)
    content: dict[str, Any] = Field(default_factory=dict)
    sensitivity: MemorySensitivity = MemorySensitivity.NORMAL
    confidence: float = Field(default=1.0, ge=0, le=1)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def session_scope_contract(self) -> "CreateMemoryRequest":
        if self.scope == MemoryScope.SESSION and not self.session_id:
            raise ValueError("session_id is required for session-scoped Memory.")
        if self.scope != MemoryScope.SESSION and self.session_id is not None:
            raise ValueError("session_id is valid only for session-scoped Memory.")
        return self


class VersionedContextMutation(ContextApiModel):
    expected_version: int = Field(ge=0)


class SupersedeMemoryRequest(VersionedContextMutation):
    replacement_memory_id: str = Field(min_length=1, max_length=128)
    replacement_expected_version: int = Field(ge=0)


class PublicMemory(ContextApiModel):
    memory_id: str
    scope: MemoryScope
    session_id: str | None
    memory_key: str
    memory_type: MemoryType
    display_text: str
    content: dict[str, Any]
    status: MemoryStatus
    sensitivity: MemorySensitivity
    confidence: float
    supersedes_memory_id: str | None
    superseded_by_memory_id: str | None
    expires_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None


class MemoryListResponse(ContextApiModel):
    memories: list[PublicMemory]


class MemorySupersedeResponse(ContextApiModel):
    superseded: PublicMemory
    replacement: PublicMemory


class PublicSkillSummary(ContextApiModel):
    name: str
    description: str
    active_version_id: str | None
    latest_version: str
    latest_status: SkillStatus


class SkillListResponse(ContextApiModel):
    skills: list[PublicSkillSummary]


class PublicSkillVersion(ContextApiModel):
    version_id: str
    name: str
    description: str
    version_label: str
    status: SkillStatus
    instructions: str
    license: str | None
    compatibility: str | None
    allowed_tools: list[str] | None
    validation_errors: list[str]
    validation_warnings: list[str]
    version: int
    created_at: datetime
    updated_at: datetime


class SkillVersionsResponse(ContextApiModel):
    name: str
    versions: list[PublicSkillVersion]


class ContextSkillUsage(ContextApiModel):
    version_id: str
    name: str
    version: str
    description: str


class ContextMemoryUsage(ContextApiModel):
    memory_id: str
    version: int
    memory_key: str
    display_text: str
    memory_type: MemoryType
    sensitivity: MemorySensitivity


class PublicContextSnapshot(ContextApiModel):
    snapshot_id: str
    status: ContextSnapshotStatus
    skills: list[ContextSkillUsage]
    memories: list[ContextMemoryUsage]
    effective_tools: list[str]
    estimated_input_tokens: int
    prepared_at: datetime
    used_at: datetime | None
    abandoned_at: datetime | None


class SessionContextResponse(ContextApiModel):
    session_id: str
    current_snapshot_id: str | None
    last_snapshot_id: str | None
    snapshots: list[PublicContextSnapshot]
