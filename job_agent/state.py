"""Shared inputs and results for the job matching workflow."""
from typing import NotRequired, TypedDict
from job_agent.schemas import JobAnalysis, ResumeAnalysis, SkillMatch


class JobAgentState(TypedDict):
    resume_text: str
    job_description: str
    resume_analysis: NotRequired[ResumeAnalysis]
    job_analysis: NotRequired[JobAnalysis]
    skill_match: NotRequired[SkillMatch]
