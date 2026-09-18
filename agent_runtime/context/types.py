"""Provider-independent context block contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, computed_field

from agent_runtime.types import RuntimeModel


class ContextBlockKind(StrEnum):
    SYSTEM_POLICY = "system_policy"
    PERMISSION_EVIDENCE_POLICY = "permission_evidence_policy"
    SKILL_PROCEDURE = "skill_procedure"
    ACTIVE_TASK = "active_task"
    REQUIRED_SOURCE_EVIDENCE = "required_source_evidence"
    MEMORY = "memory"
    CONVERSATION = "conversation"
    REQUIRED_TOOL_RESULTS = "required_tool_results"
    OPTIONAL = "optional"


class ContextTrustLevel(StrEnum):
    TRUSTED_POLICY = "trusted_policy"
    TRUSTED_PROCEDURE = "trusted_procedure"
    TRUSTED_SOURCE = "trusted_source"
    UNTRUSTED_DATA = "untrusted_data"


class ContextBlock(RuntimeModel):
    block_id: str = Field(min_length=1, max_length=160)
    kind: ContextBlockKind
    content: str = Field(min_length=1)
    mandatory: bool = False
    priority: float = 0.0
    created_at: datetime | None = None
    estimated_tokens: int = Field(default=0, ge=0)
    trust_level: ContextTrustLevel = ContextTrustLevel.UNTRUSTED_DATA


class ContextBudget(RuntimeModel):
    max_input_tokens: int = Field(gt=0)
    reserved_output_tokens: int = Field(default=0, ge=0)

    @computed_field
    @property
    def available_input_tokens(self) -> int:
        return max(0, self.max_input_tokens - self.reserved_output_tokens)


class AssembledContext(RuntimeModel):
    blocks: list[ContextBlock]
    dropped_block_ids: list[str] = Field(default_factory=list)
    estimated_tokens: int = Field(ge=0)
    budget: ContextBudget

    @property
    def text(self) -> str:
        return "\n\n".join(block.content for block in self.blocks)
