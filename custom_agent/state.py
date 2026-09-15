"""Versioned state contract for the custom agent loop."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

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


TERMINAL_STEPS = {Step.COMPLETED, Step.FAILED}


class AgentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
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

    @property
    def terminal(self) -> bool:
        return self.step in TERMINAL_STEPS

    def job_state(self) -> dict:
        data = self.model_dump(mode="json")
        data["workflow_status"] = self.status.value
        return data
