"""Shared inputs and results for the job matching workflow."""
from typing import Any, NotRequired, TypedDict

JsonObject = dict[str, Any]


class JobAgentState(TypedDict):
    # Existing fields
    resume_text: str
    job_description: str
    resume_analysis: NotRequired[JsonObject]
    job_analysis: NotRequired[JsonObject]
    skill_match: NotRequired[JsonObject]

    # New fields
    tailored_resume: NotRequired[JsonObject]
    verification: NotRequired[JsonObject | None]
    revision_feedback: NotRequired[list[str]]
    revision_count: NotRequired[int]
    max_revisions: NotRequired[int]
    approved: NotRequired[bool | None]
    human_feedback: NotRequired[str | None]
    workflow_status: NotRequired[str]
