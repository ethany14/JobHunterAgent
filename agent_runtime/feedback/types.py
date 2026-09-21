"""Versioned feedback contracts. Candidate text is data, never instructions."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeedbackSourceType(StrEnum):
    EXPLICIT_INSTRUCTION = "explicit_instruction"
    ARTIFACT_ACCEPTED = "artifact_accepted"
    ARTIFACT_REJECTED = "artifact_rejected"
    ARTIFACT_EDITED = "artifact_edited"
    EVIDENCE_CONFIRMED = "evidence_confirmed"
    EVIDENCE_REJECTED = "evidence_rejected"
    INTERVIEW_FEEDBACK = "interview_feedback"
    APPLICATION_STATUS = "application_status"
    MANUAL_FEEDBACK = "manual_feedback"


class FeedbackProcessStatus(StrEnum):
    UNPROCESSED = "unprocessed"
    NORMALIZED = "normalized"
    CLASSIFIED = "classified"
    AGGREGATED = "aggregated"
    CANDIDATE_CREATED_OR_UPDATED = "candidate_created_or_updated"
    COMPLETED = "completed"
    FAILED = "failed"


class CandidateType(StrEnum):
    PREFERENCE_MEMORY = "preference_memory"
    CAREER_EVIDENCE = "career_evidence"
    SKILL = "skill"
    PRODUCT_POLICY = "product_policy"
    IGNORE = "ignore"


class CandidateScope(StrEnum):
    APPLICATION = "application"
    USER = "user"
    ROLE_TYPE = "role_type"
    GLOBAL = "global"


class CandidateStatus(StrEnum):
    COLLECTING = "collecting"
    READY_FOR_REVIEW = "ready_for_review"
    NEEDS_CLARIFICATION = "needs_clarification"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class SignalStrength(StrEnum):
    HIGHEST = "highest"
    MEDIUM = "medium"
    WEAK = "weak"


class FeedbackEvent(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = 1
    feedback_event_id: str = Field(default_factory=lambda: str(uuid4()))
    owner_id: str
    application_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    artifact_id: str | None = None
    artifact_version: int | None = None
    source_type: FeedbackSourceType
    source_action_id: str = Field(min_length=1, max_length=160)
    original_content: str | None = Field(default=None, max_length=20_000)
    before_content: str | None = Field(default=None, max_length=20_000)
    after_content: str | None = Field(default=None, max_length=20_000)
    structured_diff_json: dict[str, Any] | None = None
    context_metadata_json: dict[str, Any] = Field(default_factory=dict)
    signal_strength: SignalStrength
    processed_status: FeedbackProcessStatus = FeedbackProcessStatus.UNPROCESSED
    processing_version: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    processed_at: datetime | None = None
    deleted_at: datetime | None = None


class FeedbackClassification(StrictModel):
    candidate_type: CandidateType
    proposed_key_or_name: str = ""
    proposed_content: str = ""
    scope: CandidateScope = CandidateScope.USER
    rationale: str
    supporting_feedback_event_ids: list[str]
    conflicting_feedback_event_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    requires_user_confirmation: bool = True
    privacy_risk: str = "normal"
    generalizability: str = "user_specific"
    proposed_expiration: datetime | None = None
    type_metadata: dict[str, Any] = Field(default_factory=dict)


class LearningCandidate(StrictModel):
    schema_version: int = 1
    candidate_id: str = Field(default_factory=lambda: str(uuid4()))
    owner_id: str
    candidate_type: CandidateType
    status: CandidateStatus
    proposed_key_or_name: str
    proposed_content: str
    scope: CandidateScope
    scope_id: str
    confidence: float = Field(ge=0, le=1)
    occurrence_count: int = Field(ge=0)
    supporting_event_ids: list[str] = Field(default_factory=list)
    conflicting_event_ids: list[str] = Field(default_factory=list)
    content_hash: str
    type_metadata: dict[str, Any] = Field(default_factory=dict)
    review_action: str | None = None
    linked_memory_id: str | None = None
    linked_evidence_id: str | None = None
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reviewed_at: datetime | None = None
    expiration_at: datetime | None = None

    @model_validator(mode="after")
    def no_auto_activation(self):
        if self.candidate_type == CandidateType.IGNORE:
            raise ValueError("Ignored feedback cannot become a candidate.")
        return self
