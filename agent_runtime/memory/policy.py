"""Deterministic policy for candidate memory and local ownership."""

from __future__ import annotations

import json
from dataclasses import dataclass

from agent_runtime.memory.types import MemoryProvenance, MemorySensitivity
from agent_runtime.types import RuntimeModel


class MemoryPolicyDecision(RuntimeModel):
    accepted_as_candidate: bool
    requires_user_confirmation: bool = True
    reason_code: str


@dataclass(frozen=True)
class LocalOwnerProfile:
    owner_id: str
    profile_id: str


class LocalOwnerResolver:
    """Server-controlled local profile selection; this is not authentication."""

    def __init__(self, *, owner_id: str = "local-user", profile_id: str = "default") -> None:
        if not owner_id.strip() or not profile_id.strip():
            raise ValueError("Local owner and profile IDs must not be blank.")
        self._profile = LocalOwnerProfile(owner_id.strip(), profile_id.strip())

    def resolve(self) -> LocalOwnerProfile:
        return self._profile


class MemoryPolicy:
    _SUSPICIOUS_PHRASES = (
        "ignore previous instructions",
        "ignore all instructions",
        "system prompt",
        "developer message",
        "execute this instruction",
        "call this tool",
        "reveal your prompt",
    )
    _NEVER_AUTO_CONFIRM_SOURCES = frozenset(
        {
            "model_inference",
            "job_description",
            "jd_requirement",
            "external_tool",
            "tool_output",
            "tool_output_instruction",
            "identity",
            "eligibility",
        }
    )

    def assess_candidate(
        self,
        *,
        display_text: str,
        content: dict,
        provenance: list[MemoryProvenance],
        sensitivity: MemorySensitivity,
    ) -> MemoryPolicyDecision:
        searchable = f"{display_text}\n{json.dumps(content, ensure_ascii=False, default=str)}".casefold()
        if any(phrase in searchable for phrase in self._SUSPICIOUS_PHRASES):
            return MemoryPolicyDecision(
                accepted_as_candidate=False,
                reason_code="suspicious_instruction_like_content",
            )
        sources = {item.source_type.casefold() for item in provenance}
        if sensitivity == MemorySensitivity.SENSITIVE:
            reason = "sensitive_requires_confirmation"
        elif sources & self._NEVER_AUTO_CONFIRM_SOURCES:
            reason = "untrusted_source_requires_confirmation"
        else:
            reason = "candidate_requires_confirmation"
        return MemoryPolicyDecision(
            accepted_as_candidate=True,
            requires_user_confirmation=True,
            reason_code=reason,
        )

    @staticmethod
    def may_enter_model_context(status: str) -> bool:
        return status == "confirmed"
