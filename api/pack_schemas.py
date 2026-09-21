"""Bounded Pack mutation requests."""
from pydantic import BaseModel, Field


class PackMutation(BaseModel):
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=128)


class GenerateRequest(PackMutation):
    max_length: int | None = Field(default=None, ge=1, le=10_000)


class QuestionRequest(GenerateRequest):
    question: str = Field(min_length=1, max_length=5000)


class EditItemRequest(PackMutation):
    content: dict
