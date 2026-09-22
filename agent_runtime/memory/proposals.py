"""Typed, deterministic memory proposals kept separate from career evidence."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from agent_runtime.types import RuntimeModel

MemoryValue = bool | int | float | str | list[str]


class MemoryProposal(RuntimeModel):
    key: str = Field(min_length=1, max_length=160)
    value: MemoryValue
    scope: Literal["global", "resume", "cover_letter", "interview", "job"]
    source_quote: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(ge=0, le=1)
    durability: Literal["session", "long_term"]
    operation: Literal["create", "update", "delete"]
    reason: str = Field(min_length=1, max_length=500)
    requires_confirmation: bool = True

    @field_validator("key")
    @classmethod
    def known_key(cls, value: str) -> str:
        allowed = {
            "resume.summary.max_sentences", "writing.style", "cover_letter.tone",
            "avoid_unsupported_metrics",
        }
        if value not in allowed:
            raise ValueError("Unknown governed memory key.")
        return value

    @model_validator(mode="after")
    def typed_value(self):
        if self.key == "resume.summary.max_sentences" and (
            isinstance(self.value, bool) or not isinstance(self.value, int) or not 1 <= self.value <= 10
        ):
            raise ValueError("resume.summary.max_sentences requires an integer from 1 to 10.")
        if self.key == "avoid_unsupported_metrics" and not isinstance(self.value, bool):
            raise ValueError("avoid_unsupported_metrics requires a boolean.")
        if self.key in {"writing.style", "cover_letter.tone"} and not isinstance(self.value, str):
            raise ValueError(f"{self.key} requires a string.")
        return self


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def extract_memory_proposals(message: str) -> list[MemoryProposal]:
    """Recognize only governed reusable rules; never use the whole message as value."""
    proposals: list[MemoryProposal] = []
    summary = re.search(
        r"(?:keep|limit|prefer)\s+(?:my\s+)?resume summary\s+(?:to|at|under|no more than)?\s*"
        r"(?P<count>\d+|one|two|three|four|five)\s+sentences?",
        message, re.I,
    )
    if summary:
        raw = summary.group("count").casefold()
        count = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
        proposals.append(MemoryProposal(
            key="resume.summary.max_sentences", value=count, scope="resume",
            source_quote=summary.group(0), confidence=1,
            durability="long_term", operation="create",
            reason="Explicit reusable resume-summary preference.",
        ))
    style = re.search(r"(?:prefer|use|keep)\s+(?:my\s+)?(?:writing|resume|cover letter)?\s*(?:style\s+)?(?:to be\s+)?(?P<style>concise|brief|direct)", message, re.I)
    if style:
        one_time = bool(re.search(r"\b(?:for this one|this time|for this job|today)\b", message, re.I))
        proposals.append(MemoryProposal(
            key="writing.style", value=style.group("style").casefold(),
            scope="job" if one_time else "global", source_quote=style.group(0),
            confidence=1, durability="session" if one_time else "long_term",
            operation="create", reason="Explicit writing-style preference.",
        ))
    tone = re.search(r"(?:cover letter\s+tone|make (?:my )?cover letter)\s+(?:to be\s+)?(?P<tone>professional|conversational|formal)", message, re.I)
    if tone:
        proposals.append(MemoryProposal(
            key="cover_letter.tone", value=tone.group("tone").casefold(),
            scope="cover_letter", source_quote=tone.group(0), confidence=1,
            durability="long_term", operation="create",
            reason="Explicit cover-letter tone preference.",
        ))
    return proposals


def proposal_conflicts(existing_content: dict, proposal: MemoryProposal,
                       *, existing_is_explicit: bool = True,
                       proposal_is_inferred: bool = False) -> bool:
    """An inferred rule never overwrites a different explicit preference."""
    current = existing_content.get("value", existing_content.get(proposal.key.split(".")[-1]))
    return current is not None and current != proposal.value and (
        existing_is_explicit or proposal_is_inferred
    )
