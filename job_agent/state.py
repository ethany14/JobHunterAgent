"""Shared inputs and results for the job matching workflow."""
from typing import NotRequired, TypedDict
from job_agent.schemas import (
    JobAnalysis,
    ResumeAnalysis,
    SkillMatch,
    TailoredResume,
    VerificationResult,
)


class JobAgentState(TypedDict):
    # Existing fields
    resume_text: str
    job_description: str
    resume_analysis: NotRequired[ResumeAnalysis]
    job_analysis: NotRequired[JobAnalysis]
    skill_match: NotRequired[SkillMatch]

    # New fields
    tailored_resume: NotRequired[TailoredResume]
    verification: NotRequired[VerificationResult | None]
    revision_feedback: NotRequired[list[str]]
    revision_count: NotRequired[int]
    max_revisions: NotRequired[int]
    approved: NotRequired[bool | None]
    human_feedback: NotRequired[str | None]
    workflow_status: NotRequired[str]
