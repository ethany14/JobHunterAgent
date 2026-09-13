import json
from unittest.mock import patch

from job_agent.schemas import (
    JobAnalysis,
    ResumeAnalysis,
    SkillEvidence,
    SkillMatch,
)
from main import analyze_files, main


def _graph_result():
    return {
        "resume_text": "Private resume source",
        "job_description": "Private job source",
        "resume_analysis": ResumeAnalysis(
            summary="Developer", skills=["Python"], experience=[], education=[]
        ),
        "job_analysis": JobAnalysis(
            title="Developer",
            summary="Build software",
            required_skills=["Python"],
            preferred_skills=[],
            responsibilities=["Build software"],
        ),
        "skill_match": SkillMatch(
            matches=[
                SkillEvidence(
                    job_skill="Python",
                    requirement_level="required",
                    matched=True,
                    resume_evidence="Python",
                    confidence=1,
                )
            ],
            missing_skills=[],
            overall_score=100,
            explanation="Python is supported.",
            recommendations=[],
        ),
    }


def test_analyze_files_reads_inputs_and_omits_raw_text(tmp_path):
    resume = tmp_path / "resume.txt"
    job = tmp_path / "job.txt"
    resume.write_text("Python developer", encoding="utf-8")
    job.write_text("Requires Python", encoding="utf-8")

    with patch("main.graph.invoke", return_value=_graph_result()) as invoke:
        result = analyze_files(resume, job)

    invoke.assert_called_once_with(
        {"resume_text": "Python developer", "job_description": "Requires Python"}
    )
    assert "resume_text" not in result
    assert "job_description" not in result
    assert result["skill_match"]["overall_score"] == 100


def test_main_writes_json_output(tmp_path):
    resume = tmp_path / "resume.txt"
    job = tmp_path / "job.txt"
    output = tmp_path / "result.json"
    resume.write_text("Python developer", encoding="utf-8")
    job.write_text("Requires Python", encoding="utf-8")

    with patch("main.graph.invoke", return_value=_graph_result()):
        exit_code = main([str(resume), str(job), "--output", str(output)])

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["job_analysis"]["title"] == "Developer"


def test_main_reports_missing_input(capsys, tmp_path):
    exit_code = main([str(tmp_path / "missing.txt"), str(tmp_path / "job.txt")])

    assert exit_code == 1
    assert "Resume file does not exist" in capsys.readouterr().err
