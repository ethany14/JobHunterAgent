"""Deterministic rendering of already-grounded structured artifacts."""
from __future__ import annotations

from job_agent.schemas import ResumeEntry, TailoredResume


def _entry_heading(entry: ResumeEntry) -> list[str]:
    lines: list[str] = []
    primary = " | ".join(item for item in (entry.heading, entry.subheading) if item)
    if primary:
        lines.append(primary)
    dates = " – ".join(item for item in (entry.start_date, entry.end_date) if item)
    secondary = " | ".join(item for item in (entry.location, dates) if item)
    if secondary:
        lines.append(secondary)
    return lines


def render_tailored_resume_text(value: TailoredResume | dict) -> str:
    resume = TailoredResume.model_validate(value)
    blocks: list[str] = []
    header = [resume.header.name or "", *resume.header.contact_lines]
    header = [line.strip() for line in header if line and line.strip()]
    if header:
        blocks.append("\n".join(header))
    for section in resume.sections:
        if not section.entries:
            continue
        lines = [section.title.upper()]
        if section.section_type == "skills":
            skills = [claim.text for entry in section.entries for claim in entry.bullets]
            if skills:
                lines.append(" • ".join(skills))
        elif section.section_type == "summary":
            claims = [claim.text for entry in section.entries for claim in entry.bullets]
            if claims:
                lines.append(" ".join(claims))
        else:
            for entry in section.entries:
                entry_lines = _entry_heading(entry)
                entry_lines.extend(f"• {claim.text}" for claim in entry.bullets)
                if entry_lines:
                    if len(lines) > 1:
                        lines.append("")
                    lines.extend(entry_lines)
        if len(lines) > 1:
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_tailored_resume(value: TailoredResume | dict) -> str:
    """Public plain-text renderer retained under the requested stable name."""
    return render_tailored_resume_text(value)


def render_tailored_resume_markdown(value: TailoredResume | dict) -> str:
    resume = TailoredResume.model_validate(value)
    blocks: list[str] = []
    header = [resume.header.name or "", *resume.header.contact_lines]
    header = [line.strip() for line in header if line and line.strip()]
    if header:
        blocks.append(f"# {header[0]}" + ("\n\n" + "  \n".join(header[1:]) if len(header) > 1 else ""))
    for section in resume.sections:
        if not section.entries:
            continue
        lines = [f"## {section.title}"]
        if section.section_type == "skills":
            skills = [claim.text for entry in section.entries for claim in entry.bullets]
            if skills:
                lines.append(" • ".join(skills))
        elif section.section_type == "summary":
            lines.append(" ".join(claim.text for entry in section.entries for claim in entry.bullets))
        else:
            for entry in section.entries:
                heading = " | ".join(item for item in (entry.heading, entry.subheading) if item)
                if heading:
                    lines.append(f"### {heading}")
                metadata = " | ".join(item for item in (
                    entry.location,
                    " – ".join(item for item in (entry.start_date, entry.end_date) if item),
                ) if item)
                if metadata:
                    lines.append(metadata)
                lines.extend(f"- {claim.text}" for claim in entry.bullets)
        if len(lines) > 1:
            blocks.append("\n\n".join(lines))
    return "\n\n".join(blocks)
