"""Immutable context snapshot contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import Field, model_validator

from agent_runtime.context.types import ContextBlockKind, ContextTrustLevel
from agent_runtime.tools.messages import AgentMessage
from agent_runtime.types import RuntimeModel


class ContextSnapshotStatus(StrEnum):
    PREPARED = "prepared"
    USED = "used"
    ABANDONED = "abandoned"


class SkillSnapshotRef(RuntimeModel):
    version_id: str
    content_hash: str


class MemorySnapshotRef(RuntimeModel):
    memory_id: str
    version: int = Field(ge=0)


class EvidenceSnapshotRef(RuntimeModel):
    evidence_id: str
    evidence_version_id: str
    version: int = Field(ge=1)
    content_hash: str


class ContextBlockManifest(RuntimeModel):
    block_id: str
    kind: ContextBlockKind
    trust_level: ContextTrustLevel
    content_hash: str
    estimated_tokens: int = Field(ge=0)


class ContextSnapshot(RuntimeModel):
    schema_version: int = 1
    snapshot_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    status: ContextSnapshotStatus = ContextSnapshotStatus.PREPARED
    system_prompt_version: str
    system_prompt_hash: str
    skill_versions: list[SkillSnapshotRef] = Field(default_factory=list)
    memory_versions: list[MemorySnapshotRef] = Field(default_factory=list)
    evidence_versions: list[EvidenceSnapshotRef] = Field(default_factory=list)
    source_artifact_ids: list[str] = Field(default_factory=list)
    effective_tools: frozenset[str] = Field(default_factory=frozenset)
    included_message_ids: list[str] = Field(default_factory=list)
    excluded_message_ids: list[str] = Field(default_factory=list)
    block_manifests: list[ContextBlockManifest]
    estimated_input_tokens: int = Field(ge=0)
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_messages: list[AgentMessage]
    prepared_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    used_at: datetime | None = None
    abandoned_at: datetime | None = None
    abandon_reason: str | None = None
    version: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def memory_manifest_matches_references(self) -> "ContextSnapshot":
        referenced = self.memory_ids
        manifested = [
            block.block_id.removeprefix("memory:")
            for block in self.block_manifests
            if block.kind == ContextBlockKind.MEMORY
        ]
        if len(referenced) != len(set(referenced)) or set(referenced) != set(manifested):
            raise ValueError(
                "Snapshot Memory references must match included Memory block manifests."
            )
        evidence_manifest = {
            block.block_id.removeprefix("evidence:")
            for block in self.block_manifests if block.kind == ContextBlockKind.CAREER_EVIDENCE
        }
        evidence_refs = {reference.evidence_id for reference in self.evidence_versions}
        if len(evidence_refs) != len(self.evidence_versions) or evidence_refs != evidence_manifest:
            raise ValueError("Snapshot Evidence references must match included Evidence blocks.")
        return self

    @property
    def memory_ids(self) -> list[str]:
        """Memory IDs whose rendered blocks are present in model_messages."""
        return [reference.memory_id for reference in self.memory_versions]


class ContextSnapshotUnavailableError(RuntimeError):
    code = "context_snapshot_unavailable"
