"""ATS-friendly PDF rendering for already-grounded tailored resumes."""

from __future__ import annotations

from html import escape
from io import BytesIO

from job_agent.schemas import ResumeEntry, ResumeSection, TailoredResume


def _safe(value: str | None) -> str:
    return escape((value or "").replace("\u2013", "-").replace("\u2014", "-").strip())


def render_tailored_resume_pdf(value: TailoredResume | dict) -> bytes:
    """Render a structured resume without adding, merging, or rewriting claims."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        HRFlowable,
        KeepTogether,
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    resume = TailoredResume.model_validate(value)
    stream = BytesIO()
    document = SimpleDocTemplate(
        stream,
        pagesize=letter,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.38 * inch,
        bottomMargin=0.38 * inch,
        title=resume.header.name or "Tailored Resume",
        author=resume.header.name or "",
    )
    body = ParagraphStyle(
        "ResumeBody", fontName="Helvetica", fontSize=9.2, leading=11.2,
        textColor=colors.HexColor("#111111"), spaceAfter=1.5,
    )
    name_style = ParagraphStyle(
        "ResumeName", parent=body, fontName="Helvetica-Bold", fontSize=18,
        leading=20, alignment=TA_CENTER, spaceAfter=2,
    )
    contact_style = ParagraphStyle(
        "ResumeContact", parent=body, fontSize=8.8, leading=10,
        alignment=TA_CENTER, spaceAfter=6,
    )
    section_style = ParagraphStyle(
        "ResumeSection", parent=body, fontName="Helvetica-Bold", fontSize=10.5,
        leading=12, spaceBefore=4, spaceAfter=0,
    )
    entry_style = ParagraphStyle(
        "ResumeEntry", parent=body, fontName="Helvetica-Bold", fontSize=9.5,
        leading=11,
    )
    meta_style = ParagraphStyle(
        "ResumeMeta", parent=body, fontSize=8.9, leading=10.5,
    )
    date_style = ParagraphStyle(
        "ResumeDate", parent=meta_style, alignment=2,
    )
    bullet_style = ParagraphStyle(
        "ResumeBullet", parent=body, leftIndent=0, firstLineIndent=0,
        fontSize=9.0, leading=10.8,
    )

    story = []
    if resume.header.name:
        story.append(Paragraph(_safe(resume.header.name), name_style))
    if resume.header.contact_lines:
        story.append(Paragraph(" &nbsp; | &nbsp; ".join(_safe(item) for item in resume.header.contact_lines if item.strip()), contact_style))

    def section_heading(section: ResumeSection) -> list:
        return [
            Spacer(1, 2),
            Paragraph(_safe(section.title).upper(), section_style),
            HRFlowable(width="100%", thickness=0.55, color=colors.HexColor("#555555"), spaceBefore=0, spaceAfter=3),
        ]

    def entry_block(entry: ResumeEntry) -> list:
        heading = " | ".join(_safe(item) for item in (entry.heading, entry.subheading) if item)
        dates = " - ".join(_safe(item) for item in (entry.start_date, entry.end_date) if item)
        rows = []
        if heading or dates:
            rows.append(Table(
                [[Paragraph(heading, entry_style), Paragraph(dates, date_style)]],
                colWidths=[document.width * 0.74, document.width * 0.26],
                style=TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ]),
            ))
        if entry.location:
            rows.append(Paragraph(_safe(entry.location), meta_style))
        if entry.bullets:
            rows.append(ListFlowable(
                [ListItem(Paragraph(_safe(claim.text), bullet_style), leftIndent=9) for claim in entry.bullets],
                bulletType="bullet", bulletFontName="Helvetica", bulletFontSize=5,
                start="square", leftIndent=11, bulletOffsetY=1.5, spaceAfter=2,
            ))
        return rows

    for section in resume.sections:
        if not section.entries:
            continue
        story.extend(section_heading(section))
        if section.section_type == "summary":
            text = " ".join(_safe(claim.text) for entry in section.entries for claim in entry.bullets)
            if text:
                story.append(Paragraph(text, body))
        elif section.section_type == "skills":
            skills = [_safe(claim.text) for entry in section.entries for claim in entry.bullets]
            if skills:
                story.append(Paragraph(" &nbsp; | &nbsp; ".join(skills), body))
        else:
            for entry in section.entries:
                block = entry_block(entry)
                if block:
                    story.append(KeepTogether(block))
                    story.append(Spacer(1, 1.5))

    document.build(story)
    return stream.getvalue()
