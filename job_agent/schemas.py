"""Validated model outputs."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResumeEvidence(AnalysisModel):
    evidence_id: str
    source_section: str
    exact_text: str


class ResumeAnalysis(AnalysisModel):
    summary: str
    skills: list[str] = Field(description="Skills evidenced in the resume.")
    evidence: list[ResumeEvidence]
    education: list[str]


class JobAnalysis(AnalysisModel):
    title: str | None = Field(description="Job title, or null when unspecified.")
    summary: str
    requirements: list["JobRequirement"]
    responsibilities: list[str]


class JobRequirement(AnalysisModel):
    requirement_id: str
    canonical_name: str
    original_text: str
    level: Literal["required", "preferred"]


class SkillEvidence(AnalysisModel):
    requirement_id: str
    job_skill: str = Field(description="Skill named in the job analysis.")
    requirement_level: Literal["required", "preferred"]
    match_status: Literal["matched", "partial", "missing"]
    resume_evidence: list[str] = Field(
        description="Exact resume evidence supporting the match; empty when missing."
    )
    confidence: float = Field(ge=0, le=1)


class SkillAssessment(AnalysisModel):
    matches: list[SkillEvidence] = Field(
        description="One evidence record for every required and preferred job skill."
    )
    explanation: str
    recommendations: list[str]


class SkillMatch(SkillAssessment):
    missing_required_skills: list[str] = Field(
        description="Required skills that are missing or only partially supported."
    )
    missing_preferred_skills: list[str] = Field(
        description="Preferred skills that are missing or only partially supported."
    )
    overall_score: float = Field(
        ge=0, le=100,
        description="Deterministic weighted skill alignment, not a hiring probability.",
    )


class SupportedClaim(AnalysisModel):
    text: str
    evidence_ids: list[str] = Field(min_length=1)


class TailoredResume(AnalysisModel):
    professional_summary: list[SupportedClaim]
    experience_bullets: list[SupportedClaim]
    highlighted_skills: list[SupportedClaim]


class UnsupportedClaim(AnalysisModel):
    claim: str
    reason: str


class VerificationResult(AnalysisModel):
    passed: bool
    unsupported_claims: list[UnsupportedClaim]
    revision_feedback: list[str]

    @model_validator(mode="after")
    def validate_verdict(self) -> "VerificationResult":
        if self.passed and (self.unsupported_claims or self.revision_feedback):
            raise ValueError("A passing verification cannot contain claims or feedback.")
        if not self.passed and not self.unsupported_claims:
            raise ValueError("A failed verification must identify unsupported claims.")
        if not self.passed and not self.revision_feedback:
            raise ValueError("A failed verification must include revision feedback.")
        return self
