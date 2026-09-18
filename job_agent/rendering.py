"""Deterministic rendering of already-grounded tailored resume claims."""
from __future__ import annotations

from job_agent.schemas import TailoredResume


def render_tailored_resume_text(value: TailoredResume | dict) -> str:
    resume = TailoredResume.model_validate(value)
    claims = (
        resume.professional_summary
        + resume.experience_bullets
        + resume.highlighted_skills
    )
    return "\n".join(claim.text for claim in claims)


def render_tailored_resume_markdown(value: TailoredResume | dict) -> str:
    resume = TailoredResume.model_validate(value)
    sections = (
        ("Professional Summary", resume.professional_summary),
        ("Experience", resume.experience_bullets),
        ("Highlighted Skills", resume.highlighted_skills),
    )
    blocks: list[str] = []
    for title, claims in sections:
        if claims:
            blocks.append(f"## {title}\n\n" + "\n".join(f"- {claim.text}" for claim in claims))
    return "\n\n".join(blocks)
