"""Validated model outputs."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResumeAnalysis(AnalysisModel):
    summary: str
    skills: list[str] = Field(description="Skills evidenced in the resume.")
    experience: list[str]
    education: list[str]


class JobAnalysis(AnalysisModel):
    title: str | None = Field(description="Job title, or null when unspecified.")
    summary: str
    required_skills: list[str]
    preferred_skills: list[str]
    responsibilities: list[str]


class SkillEvidence(AnalysisModel):
    job_skill: str = Field(description="Skill named in the job analysis.")
    requirement_level: Literal["required", "preferred"]
    matched: bool
    resume_evidence: str | None = Field(
        description="Specific supporting resume evidence, or null when unmatched."
    )
    confidence: float = Field(ge=0, le=1)


class SkillMatch(AnalysisModel):
    matches: list[SkillEvidence] = Field(
        description="One evidence record for every required and preferred job skill."
    )
    missing_skills: list[str] = Field(
        description="Required skills for which the resume provides no evidence."
    )
    overall_score: float = Field(
        ge=0, le=100,
        description="Estimated skill alignment, not a hiring probability.",
    )
    explanation: str
    recommendations: list[str]
