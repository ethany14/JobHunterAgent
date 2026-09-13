import json
from unittest.mock import patch

from job_agent.nodes import make_evidence_id
from job_agent.schemas import (
    JobAnalysis,
    JobRequirement,
    ResumeAnalysis,
    ResumeEvidence,
    SkillEvidence,
    SkillMatch,
    SupportedClaim,
    TailoredResume,
    VerificationResult,
)
from main import analyze_files, main

FACT = "Python developer"
FACT_ID = make_evidence_id(FACT)


def graph_result():
    return {
        "resume_text": FACT,
        "job_description": "Requires Python",
        "resume_analysis": ResumeAnalysis(
            summary="Developer", skills=["Python"],
            evidence=[ResumeEvidence(evidence_id=FACT_ID, source_section="Summary",
                                     exact_text=FACT)], education=[],
        ),
        "job_analysis": JobAnalysis(
            title="Developer", summary="Build software",
            requirements=[JobRequirement(requirement_id="REQ-001",
                                         canonical_name="python",
                                         original_text="Requires Python",
                                         level="required")],
            responsibilities=["Build software"],
        ),
        "skill_match": SkillMatch(
            matches=[SkillEvidence(requirement_id="REQ-001", job_skill="python",
                                   requirement_level="required", match_status="matched",
                                   resume_evidence=[FACT], confidence=1)],
            missing_required_skills=[], missing_preferred_skills=[], overall_score=100,
            explanation="Python is supported.", recommendations=[],
        ),
        "tailored_resume": TailoredResume(
            professional_summary=[SupportedClaim(text=FACT, evidence_ids=[FACT_ID])],
            experience_bullets=[SupportedClaim(text=FACT, evidence_ids=[FACT_ID])],
            highlighted_skills=[SupportedClaim(text="Python", evidence_ids=[FACT_ID])],
        ),
        "verification": VerificationResult(
            passed=True, unsupported_claims=[], revision_feedback=[]
        ),
        "revision_feedback": [], "revision_count": 0, "max_revisions": 3,
        "approved": True, "human_feedback": None, "workflow_status": "approved",
    }


def test_analyze_files_serializes_supported_claims(tmp_path):
    resume = tmp_path / "resume.txt"
    job = tmp_path / "job.txt"
    resume.write_text(FACT, encoding="utf-8")
    job.write_text("Requires Python", encoding="utf-8")
    with patch("main.graph.invoke", return_value=graph_result()):
        result = analyze_files(resume, job, thread_id="test-001")
    assert result["tailored_resume"]["professional_summary"][0]["evidence_ids"] == [FACT_ID]
    assert result["skill_match"]["missing_preferred_skills"] == []


def test_main_writes_json_output(tmp_path):
    resume = tmp_path / "resume.txt"
    job = tmp_path / "job.txt"
    output = tmp_path / "result.json"
    resume.write_text(FACT, encoding="utf-8")
    job.write_text("Requires Python", encoding="utf-8")
    with patch("main.graph.invoke", return_value=graph_result()):
        assert main([str(resume), str(job), "--thread-id", "test-002",
                     "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["verification"]["passed"] is True


def test_main_reports_missing_input(capsys, tmp_path):
    assert main([str(tmp_path / "missing.txt"), str(tmp_path / "job.txt"),
                 "--thread-id", "test-003"]) == 1
    assert "Resume file does not exist" in capsys.readouterr().err
