"""Public, structured Interviewer contracts."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class InterviewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceAssessmentStatus(StrEnum):
    SUFFICIENT = "sufficient"
    NEEDS_CLARIFICATION = "needs_clarification"
    CONFIRMED_GAP = "confirmed_gap"
    EVIDENCE_CANDIDATE = "evidence_candidate"
    EVIDENCE_CONFIRMED = "evidence_confirmed"
    SKIPPED = "skipped"
    NOT_APPLICABLE = "not_applicable"


class InterviewStatus(StrEnum):
    PLANNING = "planning"
    AWAITING_ANSWER = "awaiting_answer"
    AWAITING_EVIDENCE_CONFIRMATION = "awaiting_evidence_confirmation"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class InterviewTurnType(StrEnum):
    QUESTION = "question"
    USER_ANSWER = "user_answer"
    FOLLOW_UP = "follow_up"
    USER_SKIPPED = "user_skipped"
    USER_CONFIRMED_GAP = "user_confirmed_gap"
    CANDIDATE_PROPOSED = "candidate_proposed"
    CANDIDATE_CONFIRMED = "candidate_confirmed"
    CANDIDATE_REJECTED = "candidate_rejected"
    FOLLOW_UP_LIMIT_REACHED = "follow_up_limit_reached"


class QuestionType(StrEnum):
    EXPERIENCE = "experience"
    RESPONSIBILITY = "responsibility"
    TOOL_USAGE = "tool_usage"
    PROJECT = "project"
    LEADERSHIP = "leadership"
    METRIC_CLARIFICATION = "metric_clarification"
    DURATION_CLARIFICATION = "duration_clarification"


class AnswerOutcome(StrEnum):
    SUFFICIENT_FOR_CANDIDATE = "sufficient_for_candidate"
    NEEDS_FOLLOW_UP = "needs_follow_up"
    CONFIRMED_NO_EXPERIENCE = "confirmed_no_experience"
    USER_SKIPPED = "user_skipped"
    IRRELEVANT = "irrelevant"


class InterviewQuestion(InterviewModel):
    question_id: str
    assessment_id: str
    question: str = Field(min_length=1, max_length=1200)
    reason_for_asking: str = Field(max_length=500)
    answer_guidance: str | None = Field(default=None, max_length=500)
    question_type: QuestionType
    expects_metric: bool = False
    allows_no_experience: bool = True


class InterviewAnswerAssessment(InterviewModel):
    outcome: AnswerOutcome
    extracted_facts: list[str] = Field(default_factory=list)
    unsupported_inferences: list[str] = Field(default_factory=list)
    proposed_claim: str | None = None
    exact_supporting_quotes: list[str] = Field(default_factory=list)
    suggested_follow_up: str | None = None


class ApplicationRequirementAssessment(InterviewModel):
    assessment_id: str
    application_id: str
    snapshot_id: str
    requirement_id: str
    canonical_requirement: str
    original_requirement_text: str
    requirement_level: str
    match_status: str
    evidence_status: EvidenceAssessmentStatus
    linked_evidence_ids: list[str] = Field(default_factory=list)
    source_match_artifact_id: str
    jd_order: int
    interview_exhausted: bool = False
    version: int
    created_at: datetime
    updated_at: datetime


class InterviewTurn(InterviewModel):
    turn_id: str
    interview_session_id: str
    sequence: int
    assessment_id: str | None = None
    turn_type: InterviewTurnType
    content: str
    metadata: dict = Field(default_factory=dict)
    created_at: datetime


class InterviewSession(InterviewModel):
    interview_session_id: str
    application_id: str
    snapshot_id: str
    source_match_artifact_id: str | None = None
    agent_session_id: str
    status: InterviewStatus
    current_assessment_id: str | None = None
    pending_answer_turn_id: str | None = None
    pending_candidate_evidence_id: str | None = None
    questions_asked: int = Field(ge=0)
    max_questions: int = Field(default=10, ge=1, le=20)
    followups_for_current_requirement: int = Field(ge=0)
    max_followups_per_requirement: int = Field(default=2, ge=0, le=3)
    version: int = Field(ge=0)
    turn_sequence: int = Field(ge=0)
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
