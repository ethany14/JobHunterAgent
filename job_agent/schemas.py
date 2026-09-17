"""Validated model outputs."""
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "v3"
NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class AnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequirementCategory(StrEnum):
    SKILL = "skill"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    CERTIFICATION = "certification"
    RESPONSIBILITY = "responsibility"
    ELIGIBILITY = "eligibility"
    OTHER = "other"


class VerificationMode(StrEnum):
    RESUME_EVIDENCE = "resume_evidence"
    YEARS_EXPERIENCE = "years_experience"
    USER_CONFIRMATION = "user_confirmation"


class ResumeEvidence(AnalysisModel):
    evidence_id: NonEmptyString
    source_section: NonEmptyString
    exact_text: str

    @field_validator("exact_text")
    @classmethod
    def validate_exact_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("exact_text must not be blank.")
        return value


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
    requirement_id: NonEmptyString
    requirement_group_id: NonEmptyString
    canonical_name: NonEmptyString
    display_name: NonEmptyString
    original_text: str
    source_text: str
    atomic_text: str
    category: RequirementCategory
    verification_mode: VerificationMode
    level: Literal["required", "preferred"]
    minimum_years: int | None = Field(default=None, ge=1)

    @model_validator(mode="before")
    @classmethod
    def populate_legacy_defaults(cls, value):
        if not isinstance(value, dict):
            return value

        data = dict(value)
        requirement_id = data.get("requirement_id")
        canonical_name = data.get("canonical_name")
        original_text = data.get("original_text")

        if "requirement_group_id" not in data and requirement_id is not None:
            data["requirement_group_id"] = requirement_id
        if "display_name" not in data and canonical_name is not None:
            data["display_name"] = canonical_name
        if "source_text" not in data and original_text is not None:
            data["source_text"] = original_text
        if "atomic_text" not in data and original_text is not None:
            data["atomic_text"] = original_text
        if "category" not in data:
            data["category"] = RequirementCategory.SKILL
        if "verification_mode" not in data:
            data["verification_mode"] = (
                VerificationMode.YEARS_EXPERIENCE
                if data.get("minimum_years") is not None
                else VerificationMode.RESUME_EVIDENCE
            )
        return data


class MissingRequirement(AnalysisModel):
    canonical_name: NonEmptyString
    original_text: str
    minimum_years: int | None = Field(default=None, ge=1)


class SkillEvidence(AnalysisModel):
    requirement_id: NonEmptyString
    job_skill: NonEmptyString = Field(description="Skill named in the job analysis.")
    requirement_level: Literal["required", "preferred"]
    match_status: Literal["matched", "partial", "missing", "needs_confirmation"]
    match_reason: str | None = None
    supported_years: float | None = Field(default=None, ge=0)
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


class ScoreBreakdown(AnalysisModel):
    required_score: float | None = Field(default=None, ge=0, le=100)
    preferred_score: float | None = Field(default=None, ge=0, le=100)
    overall_score: float = Field(ge=0, le=100)


class SkillMatch(SkillAssessment):
    missing_required_requirements: list[MissingRequirement] = Field(
        description="Required requirements that are missing or only partially supported."
    )
    missing_preferred_requirements: list[MissingRequirement] = Field(
        description="Preferred requirements that are missing or only partially supported."
    )
    overall_score: float = Field(
        ge=0, le=100,
        description="Deterministic weighted skill alignment, not a hiring probability.",
    )
    score_breakdown: ScoreBreakdown
    confirmation_requirements: list[JobRequirement] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def populate_legacy_score_breakdown(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "score_breakdown" not in data and data.get("overall_score") is not None:
            data["score_breakdown"] = {
                "overall_score": data["overall_score"],
            }
        return data


class SupportedClaim(AnalysisModel):
    text: NonEmptyString
    evidence_ids: list[str] = Field(min_length=1)

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            evidence_id = value.strip()
            if not evidence_id:
                raise ValueError("Evidence IDs must not be blank.")
            if evidence_id not in cleaned:
                cleaned.append(evidence_id)
        if not cleaned:
            raise ValueError("At least one evidence ID is required.")
        return cleaned


class TailoredResume(AnalysisModel):
    professional_summary: list[SupportedClaim]
    experience_bullets: list[SupportedClaim]
    highlighted_skills: list[SupportedClaim]


class UnsupportedClaim(AnalysisModel):
    claim: NonEmptyString
    reason: NonEmptyString


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
