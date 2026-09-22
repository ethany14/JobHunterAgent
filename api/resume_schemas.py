"""Public contracts for PDF resumes and lightweight job analysis."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ResumeApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicResume(ResumeApiModel):
    resume_id: str
    filename: str
    display_name: str
    page_count: int
    is_default: bool
    created_at: datetime
    updated_at: datetime


class ResumeListResponse(ResumeApiModel):
    resumes: list[PublicResume]


class QuickAnalysisRequest(ResumeApiModel):
    job_description: str = Field(min_length=1, max_length=50_000)
    resume_id: str | None = None
    title: str | None = Field(default=None, max_length=256)
    company: str | None = Field(default=None, max_length=256)
    location: str | None = Field(default=None, max_length=256)


class AnalyzeSavedApplicationRequest(ResumeApiModel):
    expected_version: int = Field(ge=0)
    resume_id: str | None = None


class QuickAnalysisResponse(ResumeApiModel):
    analysis_id: str
    status: Literal["completed"]
    resume_id: str
    model_calls: int = Field(ge=0)
    latency_seconds: float = Field(ge=0)
    match_score: float
    matched_requirements: list[dict[str, Any]]
    partial_requirements: list[dict[str, Any]]
    missing_requirements: list[dict[str, Any]]
    confirmation_requirements: list[dict[str, Any]]
    suggestions: list[str]
