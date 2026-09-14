"""HTTP request and response models for agent runs."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RunStatus = Literal["running", "awaiting_review", "revising", "approved", "failed"]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateRunRequest(ApiModel):
    resume_text: str = Field(min_length=1)
    job_description: str = Field(min_length=1)

    @field_validator("resume_text", "job_description")
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string")
        return value


class CreateRunResponse(ApiModel):
    run_id: str
    status: Literal["running", "awaiting_review", "approved", "failed"]


class ReviewRequest(ApiModel):
    approved: bool
    feedback: str | None = None

    @model_validator(mode="after")
    def require_rejection_feedback(self) -> "ReviewRequest":
        if isinstance(self.feedback, str):
            self.feedback = self.feedback.strip() or None
        if not self.approved and not self.feedback:
            raise ValueError("feedback is required when the resume is rejected")
        return self


class RunResponse(ApiModel):
    run_id: str
    status: RunStatus
    result: dict[str, Any] | None = None
    error: str | None = None
