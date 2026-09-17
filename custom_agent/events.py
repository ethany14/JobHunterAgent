"""Append-only audit events for custom agent transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from custom_agent.state import Step


class AgentEventType(StrEnum):
    RUN_CREATED = "run_created"
    STEP_COMPLETED = "step_completed"
    PAUSED_FOR_REVIEW = "paused_for_review"
    REVIEW_APPROVED = "review_approved"
    REVIEW_REJECTED = "review_rejected"
    RUN_FAILED = "run_failed"


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    sequence: int = Field(ge=1)
    event_type: AgentEventType
    step: Step
    payload: dict = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
