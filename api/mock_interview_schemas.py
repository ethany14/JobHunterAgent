"""Public mutation contracts for mock interviews."""
from pydantic import BaseModel, Field

from agent_runtime.mock_interview.types import Difficulty, InterviewMode


class StartMockInterviewRequest(BaseModel):
    mode: InterviewMode = InterviewMode.MIXED
    difficulty: Difficulty = Difficulty.STANDARD
    target_question_count: int = Field(default=5, ge=3, le=12)
    max_followups_per_question: int = Field(default=1, ge=0, le=2)
    idempotency_key: str = Field(min_length=1, max_length=128)
    new_attempt: bool = False


class MockMutation(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class MockAnswerRequest(MockMutation):
    answer: str = Field(min_length=1, max_length=20_000)


class MockFeedbackRequest(BaseModel):
    source_action_id: str = Field(min_length=1, max_length=128)
    helpful: bool
    feedback: str | None = Field(default=None, max_length=2000)
    edited_structure: str | None = Field(default=None, max_length=5000)
