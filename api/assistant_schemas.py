"""Public contracts for the conversation-first Assistant presentation layer."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_runtime.assistant.types import AssistantActivityType, RegisteredAssistantAction


class AssistantApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicAssistantActivity(AssistantApiModel):
    activity_id: str
    session_id: str
    sequence: int
    type: AssistantActivityType
    status: str
    reference_type: str
    reference_id: str
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class TimelineResponse(AssistantApiModel):
    activities: list[PublicAssistantActivity]
    next_sequence: int


class AssistantActionRequest(AssistantApiModel):
    action_type: RegisteredAssistantAction | None = None
    utterance: str | None = Field(default=None, min_length=1, max_length=20_000)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def has_intent(self):
        if self.action_type is None and not self.utterance:
            raise ValueError("An action type or utterance is required.")
        return self


class AssistantActionResponse(AssistantApiModel):
    action_id: str
    action_type: RegisteredAssistantAction | None
    status: str
    reference_type: str | None = None
    reference_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    clarification: str | None = None
