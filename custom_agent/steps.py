"""Step handler interface used by the custom loop."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from custom_agent.state import AgentState, Step


class StepOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    updates: dict = Field(default_factory=dict)


class StepHandler(Protocol):
    def execute(self, step: Step, state: AgentState) -> StepOutcome:
        """Execute one computation step. HUMAN_REVIEW is never passed here."""
        ...
