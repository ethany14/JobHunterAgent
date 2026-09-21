"""Versioned public and internal mock-interview contracts."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InterviewMode(StrEnum):
    RECRUITER_SCREEN = "recruiter_screen"
    BEHAVIORAL = "behavioral"
    PROJECT_DEEP_DIVE = "project_deep_dive"
    ROLE_SPECIFIC = "role_specific"
    MIXED = "mixed"


class Difficulty(StrEnum):
    INTRODUCTORY = "introductory"
    STANDARD = "standard"
    CHALLENGING = "challenging"


class MockStatus(StrEnum):
    PLANNING = "planning"
    AWAITING_ANSWER = "awaiting_answer"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PlanItem(StrictModel):
    plan_item_id: str
    sequence: int = Field(ge=1)
    competency: str
    related_requirement_id: str | None = None
    related_evidence_ids: list[str] = Field(default_factory=list)
    question_type: str
    required: bool = True
    status: str = "pending"


class MockInterviewPlan(StrictModel):
    schema_version: int = 1
    plan_id: str
    mock_interview_id: str
    job_snapshot_id: str
    job_snapshot_hash: str
    pack_id: str
    pack_version: int
    evidence: list[dict]
    selected_requirement_ids: list[str]
    requirement_texts: dict[str, str] = Field(default_factory=dict)
    job_description_excerpt: str = ""
    approved_pack_excerpt: str = ""
    selected_competencies: list[str]
    prompt_version: str
    skill_version: str | None = None
    items: list[PlanItem]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MockInterviewQuestion(StrictModel):
    question_id: str
    plan_item_id: str
    question_text: str = Field(min_length=1, max_length=1000)
    question_type: str
    competency: str
    related_requirement_id: str | None = None
    related_evidence_ids: list[str] = Field(default_factory=list)
    is_followup: bool = False
    parent_question_id: str | None = None
    reason_for_asking: str
    expected_answer_elements: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MockInterviewAnswer(StrictModel):
    answer_id: str
    question_id: str
    sequence: int = Field(ge=1)
    original_text: str = Field(min_length=1, max_length=20_000)
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    idempotency_key: str


class MockAnswerEvaluation(StrictModel):
    evaluation_id: str
    answer_id: str
    overall_score: int = Field(ge=1, le=5)
    relevance_score: int = Field(ge=1, le=5)
    specificity_score: int = Field(ge=1, le=5)
    evidence_grounding_score: int = Field(ge=1, le=5)
    structure_score: int = Field(ge=1, le=5)
    communication_score: int = Field(ge=1, le=5)
    strengths: list[str] = Field(default_factory=list)
    improvement_areas: list[str] = Field(default_factory=list)
    exact_answer_quotes: list[str] = Field(default_factory=list)
    unsupported_or_unclear_claims: list[str] = Field(default_factory=list)
    missing_answer_elements: list[str] = Field(default_factory=list)
    suggested_structure: str | None = None
    followup_needed: bool = False
    followup_reason: str | None = None
    discovered_fact_quote: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def followup_has_reason(self):
        if self.followup_needed and not self.followup_reason:
            raise ValueError("Follow-up recommendation requires a reason.")
        return self


class MockInterviewSession(StrictModel):
    schema_version: int = 1
    mock_interview_id: str
    application_id: str
    root_task_id: str | None = None
    agent_session_id: str
    mode: InterviewMode
    status: MockStatus
    difficulty: Difficulty
    target_question_count: int = Field(ge=3, le=12)
    questions_completed: int = Field(default=0, ge=0)
    max_followups_per_question: int = Field(default=1, ge=0, le=2)
    current_question_id: str | None = None
    version: int = Field(default=1, ge=1)
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    error_code: str | None = None
