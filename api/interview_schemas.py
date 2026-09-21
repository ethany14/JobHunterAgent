"""Public Interviewer request contracts."""
from pydantic import BaseModel, Field


class InterviewStyleFeedbackRequest(BaseModel):
    source_action_id: str = Field(min_length=1, max_length=128)
    feedback: str = Field(min_length=1, max_length=2000)


class StartInterviewRequest(BaseModel):
    max_questions: int = Field(default=10, ge=1, le=20)
    max_followups_per_requirement: int = Field(default=2, ge=0, le=3)


class InterviewMutation(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class AnswerRequest(InterviewMutation):
    answer: str = Field(min_length=1, max_length=20_000)


class ConfirmCandidateRequest(InterviewMutation):
    edited_claim: str | None = Field(default=None, max_length=5000)
