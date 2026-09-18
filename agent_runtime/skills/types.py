"""Versioned contracts for Agent Skills-compatible packages."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from agent_runtime.types import RuntimeModel


class SkillStatus(StrEnum):
    DRAFT = "draft"
    VALIDATING = "validating"
    VALIDATED = "validated"
    APPROVAL_REQUIRED = "approval_required"
    APPROVED = "approved"
    ACTIVE = "active"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class SkillResourceKind(StrEnum):
    REFERENCE = "reference"
    ASSET = "asset"
    SCRIPT = "script"


class ParsedSkill(RuntimeModel):
    name: str
    description: str
    instructions: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    allowed_tools: frozenset[str] | None = None

    @property
    def version_label(self) -> str | None:
        return self.metadata.get("version")


class SkillValidationReport(RuntimeModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SkillResourceEntry(RuntimeModel):
    path: str
    kind: SkillResourceKind
    size_bytes: int = Field(ge=0)
    media_type: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SkillResourceManifest(RuntimeModel):
    resources: list[SkillResourceEntry] = Field(default_factory=list)
    file_count: int = Field(ge=0)
    total_size_bytes: int = Field(ge=0)


class ValidatedSkillPackage(RuntimeModel):
    package_path: Path
    parsed: ParsedSkill
    version_label: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_manifest: SkillResourceManifest
    warnings: list[str] = Field(default_factory=list)


class SkillVersion(RuntimeModel):
    schema_version: int = 1
    skill_id: str
    version_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str
    version_label: str
    status: SkillStatus = SkillStatus.DRAFT
    package_path: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    allowed_tools: frozenset[str] | None = None
    instruction_snapshot: str
    content_hash: str
    resource_manifest: SkillResourceManifest
    validation_errors: list[str] = Field(default_factory=list)
    validation_warnings: list[str] = Field(default_factory=list)
    version: int = Field(default=0, ge=0)
    event_sequence: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SkillDiscoveryRecord(RuntimeModel):
    skill_id: str
    version_id: str
    name: str
    description: str
    version: str
    status: SkillStatus


class ActiveSkill(RuntimeModel):
    skill_id: str
    version_id: str
    name: str
    description: str
    version: str
    instructions: str
    metadata: dict[str, str]
    effective_allowed_tools: frozenset[str]
    resource_manifest: SkillResourceManifest


class SkillEventType(StrEnum):
    VERSION_CREATED = "version_created"
    VALIDATION_STARTED = "validation_started"
    VALIDATED = "validated"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVED = "approved"
    ACTIVATED = "activated"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class SkillEvent(RuntimeModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    version_id: str
    sequence: int = Field(default=0, ge=0)
    event_type: SkillEventType
    from_status: SkillStatus | None = None
    to_status: SkillStatus
    payload: dict = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
