from io import BytesIO

from pypdf import PdfReader

from job_agent.pdf_rendering import render_tailored_resume_pdf


def sample_resume() -> dict:
    def claim(text: str, index: int) -> dict:
        return {
            "claim_id": f"claim-{index}",
            "text": text,
            "evidence_ids": [f"evidence-{index}"],
            "source_entry_id": "source-1",
        }

    return {
        "schema_version": 2,
        "header": {
            "name": "Alex Morgan",
            "contact_lines": ["Ithaca, NY", "alex@example.com", "linkedin.com/in/alex"],
        },
        "sections": [
            {"section_type": "education", "title": "Education", "entries": [{
                "entry_id": "education-1", "heading": "Cornell University",
                "subheading": "M.Eng., Systems Engineering", "end_date": "May 2027",
                "bullets": [],
            }]},
            {"section_type": "experience", "title": "Experience", "entries": [{
                "entry_id": "experience-1", "heading": "Northstar Labs",
                "subheading": "Software Engineer", "location": "New York, NY",
                "start_date": "2025", "end_date": "Present",
                "bullets": [
                    claim("Built evidence-grounded application workflows with Python and FastAPI.", 1),
                    claim("Added deterministic validation and durable human review checkpoints.", 2),
                ],
            }]},
            {"section_type": "projects", "title": "Projects", "entries": [{
                "entry_id": "project-1", "heading": "JobHunterAgent",
                "subheading": "AI Application Platform", "start_date": "2026",
                "end_date": "Present", "bullets": [claim(
                    "Created a local career workspace with persisted runs and versioned artifacts.", 3
                )],
            }]},
            {"section_type": "skills", "title": "Skills", "entries": [{
                "entry_id": "skills-1", "bullets": [
                    claim("Python, SQL, Java", 4), claim("FastAPI, SQLAlchemy, Docker", 5),
                ],
            }]},
        ],
        "quality_adjustments": [],
    }


def test_tailored_resume_pdf_is_letter_sized_searchable_and_grounded() -> None:
    content = render_tailored_resume_pdf(sample_resume())
    assert content.startswith(b"%PDF")
    reader = PdfReader(BytesIO(content))
    assert len(reader.pages) == 1
    assert round(float(reader.pages[0].mediabox.width)) == 612
    assert round(float(reader.pages[0].mediabox.height)) == 792
    text = reader.pages[0].extract_text()
    assert "Alex Morgan" in text
    assert "EDUCATION" in text
    assert "Built evidence-grounded application workflows" in text
    assert "evidence-1" not in text
    assert "claim-1" not in text
