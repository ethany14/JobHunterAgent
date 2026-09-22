"""Validated model outputs."""
import hashlib
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
    source_entry_id: str | None = None

    @field_validator("exact_text")
    @classmethod
    def validate_exact_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("exact_text must not be blank.")
        return value


class ResumeSourceEntry(AnalysisModel):
    source_entry_id: NonEmptyString
    entry_type: Literal["summary", "experience", "project", "education", "skills", "other"]
    heading: str | None = None
    organization: str | None = None
    location: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class ResumeAnalysis(AnalysisModel):
    summary: str
    skills: list[str] = Field(description="Skills evidenced in the resume.")
    evidence: list[ResumeEvidence]
    education: list[str]
    source_entries: list[ResumeSourceEntry] = Field(default_factory=list)


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
    claim_id: str = ""
    text: NonEmptyString
    evidence_ids: list[str] = Field(min_length=1)
    source_entry_id: str = "legacy:unattributed"
    target_requirement_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def populate_compatibility_identity(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        text = str(data.get("text") or "").strip()
        evidence = ",".join(sorted(str(item).strip() for item in data.get("evidence_ids", [])))
        if not data.get("claim_id") and text:
            digest = hashlib.sha256(f"{text.casefold()}|{evidence}".encode()).hexdigest()[:12]
            data["claim_id"] = f"CLM-{digest}"
        data.setdefault("source_entry_id", "legacy:unattributed")
        return data

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


class ResumeHeader(AnalysisModel):
    name: str | None = None
    contact_lines: list[str] = Field(default_factory=list)


class ResumeEntry(AnalysisModel):
    entry_id: NonEmptyString
    heading: str | None = None
    subheading: str | None = None
    location: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    bullets: list[SupportedClaim] = Field(default_factory=list)


class ResumeSection(AnalysisModel):
    section_type: Literal["summary", "experience", "projects", "education", "skills"]
    title: NonEmptyString
    entries: list[ResumeEntry] = Field(default_factory=list)


def upgrade_tailored_resume_v1_to_v2(value: dict) -> dict:
    """Explicitly upgrade the three-list v1 contract without inventing metadata."""
    expected = {"professional_summary", "experience_bullets", "highlighted_skills"}
    if not expected.issubset(value):
        raise ValueError("Legacy tailored resume is missing required v1 fields.")
    mapping = (
        ("summary", "Professional Summary", "professional_summary", "legacy:summary"),
        ("experience", "Experience", "experience_bullets", "legacy:experience"),
        ("skills", "Skills", "highlighted_skills", "legacy:skills"),
    )
    sections = []
    for section_type, title, field, entry_id in mapping:
        claims = []
        for index, raw in enumerate(value.get(field) or [], start=1):
            claim = dict(raw) if isinstance(raw, dict) else raw.model_dump(mode="python")
            claim.setdefault("source_entry_id", entry_id)
            if not claim.get("claim_id"):
                evidence = ",".join(sorted(str(item) for item in claim.get("evidence_ids", [])))
                digest = hashlib.sha256(
                    f"{field}|{index}|{str(claim.get('text', '')).casefold()}|{evidence}".encode()
                ).hexdigest()[:12]
                claim["claim_id"] = f"CLM-{digest}"
            claims.append(claim)
        sections.append({
            "section_type": section_type,
            "title": title,
            "entries": [{"entry_id": entry_id, "bullets": claims}] if claims else [],
        })
    return {
        "schema_version": 2,
        "header": value.get("header") or {},
        "sections": sections,
        "quality_adjustments": value.get("quality_adjustments") or [],
    }


class TailoredResume(AnalysisModel):
    schema_version: Literal[2] = 2
    header: ResumeHeader = Field(default_factory=ResumeHeader)
    sections: list[ResumeSection]
    quality_adjustments: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def upgrade_v1(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return value
        if value.get("schema_version") == 2 or "sections" in value:
            return value
        return upgrade_tailored_resume_v1_to_v2(value)

    def claims(self) -> list[SupportedClaim]:
        return [claim for section in self.sections for entry in section.entries for claim in entry.bullets]

    def claims_for(self, section_type: str) -> list[SupportedClaim]:
        return [claim for section in self.sections if section.section_type == section_type
                for entry in section.entries for claim in entry.bullets]

    @property
    def professional_summary(self) -> list[SupportedClaim]:
        return self.claims_for("summary")

    @property
    def experience_bullets(self) -> list[SupportedClaim]:
        return self.claims_for("experience") + self.claims_for("projects")

    @property
    def highlighted_skills(self) -> list[SupportedClaim]:
        return self.claims_for("skills")


class ResumeQualityIssue(AnalysisModel):
    code: Literal[
        "duplicate_claim", "summary_too_long", "summary_repeats_bullet",
        "duplicate_skill", "invalid_section_membership", "unknown_source_entry",
        "empty_section", "unformatted_skill_block", "claim_without_evidence",
        "unknown_evidence_id",
    ]
    message: NonEmptyString
    claim_ids: list[str] = Field(default_factory=list)
    section_type: str | None = None


class ResumeQualityResult(AnalysisModel):
    passed: bool
    issues: list[ResumeQualityIssue] = Field(default_factory=list)
    revision_feedback: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent(self):
        if self.passed == bool(self.issues):
            raise ValueError("Quality verdict and issues disagree.")
        return self


class UnsupportedClaim(AnalysisModel):
    claim: NonEmptyString
    reason: NonEmptyString


class VerificationResult(AnalysisModel):
    passed: bool
    unsupported_claims: list[UnsupportedClaim]
    revision_feedback: list[str]
    quality: ResumeQualityResult | None = None

    @model_validator(mode="after")
    def validate_verdict(self) -> "VerificationResult":
        if self.passed and (self.unsupported_claims or self.revision_feedback):
            raise ValueError("A passing verification cannot contain claims or feedback.")
        if not self.passed and not self.unsupported_claims and not (
            self.quality and self.quality.issues
        ):
            raise ValueError("A failed verification must identify factual or quality issues.")
        if not self.passed and not self.revision_feedback:
            raise ValueError("A failed verification must include revision feedback.")
        return self
