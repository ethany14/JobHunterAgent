"""Bounded public contracts for opt-in Job workflow execution."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MultiRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_application_version: int = Field(ge=0)
    include_interview: bool = False
    requested_artifacts: list[Literal["tailored_resume", "cover_letter", "application_answer"]] = Field(
        min_length=1, max_length=3)
    application_questions: list[str] = Field(default_factory=list, max_length=3)
    budget_profile: Literal["standard", "extended"] = "standard"
    idempotency_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_selection(self):
        if len(set(self.requested_artifacts)) != len(self.requested_artifacts):
            raise ValueError("Requested artifact types must be unique.")
        if ("application_answer" in self.requested_artifacts) != bool(self.application_questions):
            raise ValueError("Application answers require explicit questions.")
        if any(not question.strip() or len(question) > 5000 for question in self.application_questions):
            raise ValueError("Questions must contain 1 to 5,000 characters.")
        return self


class MultiRunMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)


class MultiRunResume(MultiRunMutation):
    task_id: str = Field(min_length=1, max_length=36)
    continue_without_clarification: bool = False
