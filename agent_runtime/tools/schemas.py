"""Input and output contracts for the built-in Job Agent tools."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import Field, field_validator
from agent_runtime.types import RuntimeModel
from job_agent.schemas import JobAnalysis, ResumeAnalysis, SkillMatch, TailoredResume, VerificationResult

RunStatus = Literal["running", "awaiting_review", "revising", "approved", "failed"]

class PublicRunResult(RuntimeModel):
    resume_analysis: ResumeAnalysis
    job_analysis: JobAnalysis
    skill_match: SkillMatch
    tailored_resume: TailoredResume
    verification: VerificationResult
    revision_feedback: list[str]
    revision_count: int = Field(ge=0)
    max_revisions: int = Field(ge=0)

class ListRecentRunsInput(RuntimeModel):
    limit: int = Field(default=10, ge=1, le=100)

class RecentRunSummary(RuntimeModel):
    run_id: str
    status: RunStatus
    job_title: str | None
    match_score: float | None = Field(default=None, ge=0, le=100)
    missing_required_count: int | None = Field(default=None, ge=0)
    created_at: datetime
    updated_at: datetime

class ListRecentRunsOutput(RuntimeModel):
    runs: list[RecentRunSummary]

class GetRunResultInput(RuntimeModel):
    run_id: str = Field(min_length=1)

class CompareRunRequirementsInput(RuntimeModel):
    run_ids: list[str] = Field(min_length=2, max_length=20)

    @field_validator("run_ids")
    @classmethod
    def require_unique_run_ids(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("run_ids must contain distinct, non-empty IDs")
        return cleaned

class RunRequirementComparison(RuntimeModel):
    run_id: str
    score: float = Field(ge=0, le=100)
    unique_missing_requirements: list[str]
    partial_requirements: list[str]
    confirmation_requirements: list[str]

class CompareRunRequirementsOutput(RuntimeModel):
    common_required_requirements: list[str]
    common_missing_requirements: list[str]
    common_partial_requirements: list[str]
    recurring_matched_requirements: list[str]
    runs: list[RunRequirementComparison]

class RenderTailoredResumeInput(RuntimeModel):
    run_id: str = Field(min_length=1)
    format: Literal["text", "markdown"] = "markdown"

class RenderTailoredResumeOutput(RuntimeModel):
    run_id: str
    format: Literal["text", "markdown"]
    content: str
