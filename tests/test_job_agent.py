from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from job_agent.agent import graph
from job_agent.nodes import (
    _create_model,
    analyze_job,
    analyze_resume,
    calculate_match_score,
    make_evidence_id,
    match_skills,
    validate_extracted_evidence,
    validate_input,
    verify_resume,
)
from job_agent.routes import route_after_verification
from job_agent.schemas import (
    JobAnalysis,
    JobRequirement,
    ResumeAnalysis,
    ResumeEvidence,
    SkillAssessment,
    SkillEvidence,
    SkillMatch,
    SupportedClaim,
    TailoredResume,
    UnsupportedClaim,
    VerificationResult,
)

FACT = "Built REST APIs in Python and FastAPI."
FACT_ID = make_evidence_id(FACT)


def resume_analysis(exact_text: str = FACT) -> ResumeAnalysis:
    return ResumeAnalysis(
        summary="Developer",
        skills=["Python", "FastAPI"],
        evidence=[ResumeEvidence(evidence_id="temporary", source_section="Experience",
                                 exact_text=exact_text)],
        education=[],
    )


def job_analysis() -> JobAnalysis:
    return JobAnalysis(
        title="Developer",
        summary="Build services",
        requirements=[
            JobRequirement(requirement_id="REQ-001", canonical_name="python",
                           original_text="Strong Python skills", level="required"),
            JobRequirement(requirement_id="REQ-002", canonical_name="sql",
                           original_text="SQL experience", level="required"),
        ],
        responsibilities=["Build services"],
    )


def skill_assessment() -> SkillAssessment:
    return SkillAssessment(
        matches=[
            SkillEvidence(requirement_id="REQ-001", job_skill="python",
                          requirement_level="required", match_status="matched",
                          resume_evidence=[FACT], confidence=1),
            SkillEvidence(requirement_id="REQ-002", job_skill="sql",
                          requirement_level="required", match_status="missing",
                          resume_evidence=[], confidence=1),
        ],
        explanation="Python is evidenced; SQL is not.",
        recommendations=["Add SQL evidence if applicable."],
    )


def claim(text: str, evidence_id: str = FACT_ID) -> SupportedClaim:
    return SupportedClaim(text=text, evidence_ids=[evidence_id])


def tailored(summary: str = "Python developer", evidence_id: str = FACT_ID) -> TailoredResume:
    return TailoredResume(
        professional_summary=[claim(summary, evidence_id)],
        experience_bullets=[claim("Built REST APIs in Python and FastAPI.", evidence_id)],
        highlighted_skills=[claim("Python", evidence_id), claim("FastAPI", evidence_id)],
    )


def passing_verification() -> VerificationResult:
    return VerificationResult(passed=True, unsupported_claims=[], revision_feedback=[])


def failed_verification(claim_text: str = "AWS") -> VerificationResult:
    return VerificationResult(
        passed=False,
        unsupported_claims=[UnsupportedClaim(claim=claim_text,
                                              reason="No supporting resume evidence.")],
        revision_feedback=[f"Remove {claim_text}"],
    )


def configure_model(tmp_path, monkeypatch, side_effect):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_MODEL_ID=test-model\n")
    monkeypatch.setattr("job_agent.nodes.ENV_PATH", env_file)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    structured = MagicMock()
    structured.invoke.side_effect = side_effect
    return structured


def test_full_workflow_grounds_all_claims_and_scores_in_python(tmp_path, monkeypatch):
    structured = configure_model(
        tmp_path, monkeypatch,
        [resume_analysis(), job_analysis(), skill_assessment(), tailored(),
         passing_verification()],
    )
    with patch("job_agent.nodes.ChatOpenAI") as model:
        model.return_value.with_structured_output.return_value = structured
        result = graph.invoke({"resume_text": FACT,
                               "job_description": "Python and SQL required"})
    assert result["resume_analysis"].evidence[0].evidence_id == FACT_ID
    assert result["skill_match"].overall_score == 50.0
    assert result["skill_match"].missing_required_skills == ["sql"]
    assert result["skill_match"].missing_preferred_skills == []
    assert all(item.evidence_ids for item in result["tailored_resume"].professional_summary)
    assert result["verification"].passed is True
    schemas = [call.args[0] for call in model.return_value.with_structured_output.call_args_list]
    assert schemas == [ResumeAnalysis, JobAnalysis, SkillAssessment,
                       TailoredResume, VerificationResult]


def test_extracted_evidence_must_exist_in_original_resume():
    state = {"resume_text": FACT, "resume_analysis": resume_analysis("Invented AWS work")}
    with pytest.raises(ValueError, match="Evidence is not present"):
        validate_extracted_evidence(state)


def test_python_assigns_stable_evidence_ids_and_deduplicates():
    analysis = resume_analysis()
    analysis.evidence.append(
        ResumeEvidence(evidence_id="duplicate", source_section="Skills", exact_text=FACT)
    )
    with patch("job_agent.nodes._analyze", return_value=analysis):
        first = analyze_resume({"resume_text": FACT}, {})["resume_analysis"]
        second = analyze_resume({"resume_text": FACT}, {})["resume_analysis"]
    assert len(first.evidence) == 1
    assert first.evidence[0].evidence_id == second.evidence[0].evidence_id == FACT_ID


def test_job_requirements_are_deduplicated_and_required_wins():
    raw = JobAnalysis(
        title="Engineer", summary="Role", responsibilities=[],
        requirements=[
            JobRequirement(requirement_id="x", canonical_name="Python",
                           original_text="Python", level="preferred"),
            JobRequirement(requirement_id="y", canonical_name=" python ",
                           original_text="Strong Python programming skills", level="required"),
            JobRequirement(requirement_id="z", canonical_name="Kubernetes",
                           original_text="Docker and Kubernetes", level="preferred"),
            JobRequirement(requirement_id="q", canonical_name="kubernetes",
                           original_text="Kubernetes required", level="required"),
        ],
    )
    with patch("job_agent.nodes._analyze", return_value=raw):
        result = analyze_job({"job_description": "Role"}, {})["job_analysis"]
    assert [(item.canonical_name, item.level) for item in result.requirements] == [
        ("python", "required"), ("kubernetes", "required")
    ]
    assert [item.requirement_id for item in result.requirements] == ["REQ-001", "REQ-002"]


def test_calculate_match_score_uses_fixed_weights():
    matches = [
        SkillEvidence(requirement_id="1", job_skill="python", requirement_level="required",
                      match_status="matched", resume_evidence=[FACT], confidence=1),
        SkillEvidence(requirement_id="2", job_skill="sql", requirement_level="required",
                      match_status="partial", resume_evidence=[FACT], confidence=.5),
        SkillEvidence(requirement_id="3", job_skill="aws", requirement_level="preferred",
                      match_status="missing", resume_evidence=[], confidence=1),
    ]
    assert calculate_match_score(matches) == 60.0
    assert calculate_match_score([]) == 0.0


def test_match_downgrades_evidence_not_in_extracted_source():
    resume = resume_analysis()
    resume.evidence[0].evidence_id = FACT_ID
    assessment = SkillAssessment(
        matches=[SkillEvidence(requirement_id="REQ-001", job_skill="python",
                               requirement_level="required", match_status="matched",
                               resume_evidence=["Invented evidence"], confidence=.9)],
        explanation="Claimed match", recommendations=[],
    )
    with patch("job_agent.nodes._analyze", return_value=assessment):
        result = match_skills({"resume_analysis": resume,
                               "job_analysis": job_analysis()}, {})["skill_match"]
    assert result.matches[0].match_status == "missing"
    assert result.missing_required_skills == ["python", "sql"]


def test_unknown_evidence_id_in_summary_forces_failure():
    resume = resume_analysis()
    resume.evidence[0].evidence_id = FACT_ID
    fake = tailored(summary="AWS leader", evidence_id="EXP-unknown")
    with patch("job_agent.nodes._analyze", return_value=passing_verification()):
        result = verify_resume(
            {"resume_text": FACT, "job_description": "AWS role",
             "resume_analysis": resume, "tailored_resume": fake}, {}
        )
    assert result["verification"].passed is False
    assert "EXP-unknown" in result["verification"].unsupported_claims[0].reason


def test_deliberately_false_resume_is_rejected_with_specific_claims():
    fake = TailoredResume(
        professional_summary=[claim(
            "Backend engineer with five years of leadership experience deploying Kubernetes services to AWS."
        )],
        experience_bullets=[claim("Increased company revenue by 30%.")],
        highlighted_skills=[claim("AWS"), claim("Kubernetes")],
    )
    expected = VerificationResult(
        passed=False,
        unsupported_claims=[
            UnsupportedClaim(claim="five years of leadership experience",
                             reason="No supporting resume evidence."),
            UnsupportedClaim(claim="deploying Kubernetes services to AWS",
                             reason="No AWS or Kubernetes evidence."),
            UnsupportedClaim(claim="Increased company revenue by 30%",
                             reason=f"{FACT_ID} does not support this claim."),
        ],
        revision_feedback=["Remove leadership, AWS, Kubernetes, and revenue claims."],
    )
    resume = resume_analysis()
    resume.evidence[0].evidence_id = FACT_ID
    with patch("job_agent.nodes._analyze", return_value=expected) as analyze:
        result = verify_resume(
            {"resume_text": FACT, "job_description": "AWS Kubernetes leader",
             "resume_analysis": resume, "tailored_resume": fake}, {}
        )
    content = analyze.call_args.args[2]
    assert "SOURCE OF TRUTH - ORIGINAL RESUME" in content
    assert "JOB DESCRIPTION (NOT EVIDENCE)" in content
    assert result["verification"].unsupported_claims == expected.unsupported_claims


@pytest.mark.parametrize("inputs", [
    {}, {"resume_text": " ", "job_description": "Python"},
    {"resume_text": "Python", "job_description": " "},
])
def test_invalid_inputs_do_not_call_model(inputs):
    with patch("job_agent.nodes.ChatOpenAI") as model:
        with pytest.raises(ValueError, match="non-empty string"):
            graph.invoke(inputs)
        model.assert_not_called()


def test_route_after_verification():
    assert route_after_verification(
        {"verification": passing_verification(), "revision_count": 0, "max_revisions": 3}
    ) == "end"
    assert route_after_verification(
        {"verification": failed_verification(), "revision_count": 0, "max_revisions": 3}
    ) == "revise"
    assert route_after_verification(
        {"verification": failed_verification(), "revision_count": 3, "max_revisions": 3}
    ) == "end"


def test_graph_revises_then_passes(tmp_path, monkeypatch):
    draft = tailored(summary="AWS leader")
    revised = tailored(summary="Python developer")
    structured = configure_model(
        tmp_path, monkeypatch,
        [resume_analysis(), job_analysis(), skill_assessment(), draft,
         failed_verification("AWS leader"), revised, passing_verification()],
    )
    with patch("job_agent.nodes.ChatOpenAI") as model:
        model.return_value.with_structured_output.return_value = structured
        result = graph.invoke({"resume_text": FACT,
                               "job_description": "Python and SQL required"})
    assert result["revision_count"] == 1
    assert result["verification"].passed is True


def test_graph_stops_after_three_revisions(tmp_path, monkeypatch):
    draft = tailored(summary="Unsupported claim")
    failed = failed_verification("Unsupported claim")
    structured = configure_model(
        tmp_path, monkeypatch,
        [resume_analysis(), job_analysis(), skill_assessment(), draft, failed,
         draft, failed, draft, failed, draft, failed],
    )
    with patch("job_agent.nodes.ChatOpenAI") as model:
        model.return_value.with_structured_output.return_value = structured
        result = graph.invoke({"resume_text": FACT,
                               "job_description": "Python and SQL required"})
    assert result["revision_count"] == 3
    assert result["verification"].passed is False


def test_model_environment_overrides_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr("job_agent.nodes.ENV_PATH", env_file)
    monkeypatch.setenv("LLM_MODEL_ID", "deployed-model")
    env_file.write_text("LLM_MODEL_ID=file-model\nLLM_API_KEY=test-key\n")
    with patch("job_agent.nodes.ChatOpenAI") as model:
        _create_model()
    assert model.call_args.kwargs["model"] == "deployed-model"


def test_validate_input_initializes_and_limits_revisions():
    assert validate_input({"resume_text": "Resume", "job_description": "Job"})[
        "max_revisions"
    ] == 3
    with pytest.raises(ValueError, match="max_revisions"):
        validate_input({"resume_text": "Resume", "job_description": "Job",
                        "max_revisions": 4})


def test_verification_verdict_must_be_consistent():
    with pytest.raises(ValidationError, match="passing verification"):
        VerificationResult(
            passed=True,
            unsupported_claims=[UnsupportedClaim(claim="AWS", reason="No evidence")],
            revision_feedback=[],
        )
