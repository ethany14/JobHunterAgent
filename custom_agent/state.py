"""Versioned state contract for the custom agent loop."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from job_agent.schemas import (
    JobAnalysis,
    ResumeAnalysis,
    SkillMatch,
    TailoredResume,
    VerificationResult,
)


class Step(StrEnum):
    VALIDATE_INPUT = "validate_input"
    ANALYZE_RESUME = "analyze_resume"
    VALIDATE_EVIDENCE = "validate_extracted_evidence"
    ANALYZE_JOB = "analyze_job"
    MATCH_SKILLS = "match_skills"
    WRITE_RESUME = "write_resume"
    VERIFY_RESUME = "verify_resume"
    REVISE_RESUME = "revise_resume"
    HUMAN_REVIEW = "human_review"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentStatus(StrEnum):
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    REVISING = "revising"
    APPROVED = "approved"
    FAILED = "failed"


TERMINAL_STEPS = frozenset({Step.COMPLETED, Step.FAILED})


class AgentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    PUBLIC_RESULT_FIELDS: ClassVar[set[str]] = {
        "resume_analysis",
        "job_analysis",
        "skill_match",
        "tailored_resume",
        "verification",
        "revision_feedback",
        "revision_count",
        "max_revisions",
    }

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    step: Step = Step.VALIDATE_INPUT
    status: AgentStatus = AgentStatus.RUNNING
    version: int = Field(default=0, ge=0)
    event_sequence: int = Field(default=0, ge=0)

    resume_text: str
    job_description: str
    resume_analysis: ResumeAnalysis | None = None
    job_analysis: JobAnalysis | None = None
    skill_match: SkillMatch | None = None
    tailored_resume: TailoredResume | None = None
    verification: VerificationResult | None = None

    revision_feedback: list[str] = Field(default_factory=list)
    revision_count: int = Field(default=0, ge=0)
    max_revisions: int = Field(default=3, ge=0, le=3)
    approved: bool | None = None
    human_feedback: str | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("run_id must not be empty.")
        return cleaned

    @model_validator(mode="after")
    def validate_state_invariants(self) -> Self:
        special_statuses = {
            Step.REVISE_RESUME: AgentStatus.REVISING,
            Step.HUMAN_REVIEW: AgentStatus.AWAITING_REVIEW,
            Step.COMPLETED: AgentStatus.APPROVED,
            Step.FAILED: AgentStatus.FAILED,
        }
        expected_status = special_statuses.get(self.step, AgentStatus.RUNNING)
        if self.status != expected_status:
            raise ValueError(
                f"Step '{self.step.value}' requires status "
                f"'{expected_status.value}', not '{self.status.value}'."
            )
        if self.step == Step.COMPLETED:
            if self.approved is not True:
                raise ValueError("A completed run must have approved=True.")
            if self.verification is None:
                raise ValueError("A completed run must have a verification result.")
        if self.approved is True and self.step != Step.COMPLETED:
            raise ValueError("approved=True is only valid for a completed run.")
        if self.step == Step.HUMAN_REVIEW and self.verification is None:
            raise ValueError("Human review requires a verification result.")
        if self.step == Step.FAILED and not self.error_message:
            raise ValueError("A failed run must contain a safe error message.")
        return self

    @property
    def terminal(self) -> bool:
        return self.step in TERMINAL_STEPS

    def job_state(self) -> dict[str, Any]:
        """Return only fields accepted by the public result projector."""
        return self.model_dump(mode="json", include=self.PUBLIC_RESULT_FIELDS)
