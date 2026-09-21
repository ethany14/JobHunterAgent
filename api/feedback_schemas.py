"""Public feedback API inputs; owner and permissions are never client supplied."""
from pydantic import BaseModel, Field

from agent_runtime.feedback.types import FeedbackSourceType


class CreateFeedbackRequest(BaseModel):
    source_type: FeedbackSourceType
    source_action_id: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=20_000)
    application_id: str | None = None
    session_id: str | None = None


class CandidateMutationRequest(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    content: str | None = Field(default=None, max_length=20_000)
