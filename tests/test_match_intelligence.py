from unittest.mock import patch

import pytest

from custom_agent.handlers import JobAgentStepHandler
from custom_agent.state import AgentState, Step
from job_agent.domain import normalize_skill_match
from job_agent.nodes import match_skills
from job_agent.prompts import MATCH_PROMPT
from job_agent.schemas import (
    JobAnalysis,
    JobRequirement,
    RequirementCategory,
    ResumeAnalysis,
    ResumeEvidence,
    SkillAssessment,
    SkillEvidence,
    SkillMatch,
    VerificationMode,
)


def requirement(
    name: str,
    *,
    category: RequirementCategory = RequirementCategory.SKILL,
    verification_mode: VerificationMode = VerificationMode.RESUME_EVIDENCE,
    minimum_years: int | None = None,
) -> JobRequirement:
    return JobRequirement(
        requirement_id=f"REQ-{name}",
        requirement_group_id=f"REQG-{name}",
        canonical_name=name,
        display_name=name.replace("_", " ").title(),
        original_text=name.replace("_", " "),
        source_text=name.replace("_", " "),
        atomic_text=name.replace("_", " "),
        category=category,
        verification_mode=verification_mode,
        level="required",
        minimum_years=minimum_years,
    )


def resume_with(*facts: str) -> ResumeAnalysis:
    return ResumeAnalysis(
        summary="Candidate",
        skills=[],
        evidence=[
            ResumeEvidence(
                evidence_id=f"EXP-{index}",
                source_section="Resume",
                exact_text=fact,
            )
            for index, fact in enumerate(facts, start=1)
        ],
        education=[],
    )


def assessment_for(
    requirement_: JobRequirement,
    *,
    status: str = "matched",
    evidence: list[str] | None = None,
    supported_years: float | None = None,
) -> SkillAssessment:
    return SkillAssessment(
        matches=[
            SkillEvidence(
                requirement_id=requirement_.requirement_id,
                job_skill=requirement_.canonical_name,
                requirement_level=requirement_.level,
                match_status=status,
                resume_evidence=evidence or [],
                supported_years=supported_years,
                confidence=0.9,
            )
        ],
        explanation="Model assessment",
        recommendations=[],
    )


def normalize_one(
    requirement_: JobRequirement,
    resume: ResumeAnalysis,
    assessment: SkillAssessment,
) -> SkillMatch:
    job = JobAnalysis(
        title="Role",
        summary="Role",
        requirements=[requirement_],
        responsibilities=[],
    )
    return normalize_skill_match(resume, job, assessment)


def test_match_prompt_defines_all_four_statuses_and_deterministic_boundaries():
    normalized = " ".join(MATCH_PROMPT.split())

    for status in ("matched", "partial", "missing", "needs_confirmation"):
        assert status in normalized
    assert "Tableau does not prove Power BI" in normalized
    assert "general AI experience does not prove Claude or Claude Code" in normalized
    assert "currently pursued" in normalized
    assert "minimum number of years" in normalized


def test_user_confirmation_is_separate_from_missing_requirements():
    work_authorization = requirement(
        "work_authorization",
        category=RequirementCategory.ELIGIBILITY,
        verification_mode=VerificationMode.USER_CONFIRMATION,
    )
    result = normalize_one(
        work_authorization,
        resume_with(),
        assessment_for(work_authorization, status="missing"),
    )

    assert result.matches[0].match_status == "needs_confirmation"
    assert result.confirmation_requirements == [work_authorization]
    assert result.missing_required_requirements == []
    assert result.missing_preferred_requirements == []


@pytest.mark.parametrize("model_status", ["matched", "partial"])
def test_matched_or_partial_without_valid_resume_evidence_becomes_missing(model_status):
    python = requirement("python")
    result = normalize_one(
        python,
        resume_with("Built Python APIs."),
        assessment_for(python, status=model_status, evidence=["Invented Python work"]),
    )

    match = result.matches[0]
    assert match.match_status == "missing"
    assert match.resume_evidence == []
    assert match.confidence == 0
    assert result.missing_required_requirements[0].canonical_name == "python"


def test_in_progress_education_cannot_be_a_full_match():
    fact = "Currently pursuing a Bachelor of Science; expected graduation 2027."
    degree = requirement("bachelors_degree", category=RequirementCategory.EDUCATION)
    result = normalize_one(
        degree,
        resume_with(fact),
        assessment_for(degree, evidence=[fact]),
    )

    assert result.matches[0].match_status == "partial"
    assert result.matches[0].match_reason == "The cited education is still in progress."


@pytest.mark.parametrize(
    ("fact", "expected_years"),
    [
        ("Built Python APIs.", None),
        ("Three years of Python experience.", 3),
    ],
)
def test_unsupported_or_insufficient_years_cannot_be_a_full_match(
    fact, expected_years
):
    python = requirement(
        "python",
        category=RequirementCategory.EXPERIENCE,
        verification_mode=VerificationMode.YEARS_EXPERIENCE,
        minimum_years=5,
    )
    result = normalize_one(
        python,
        resume_with(fact),
        assessment_for(python, evidence=[fact], supported_years=10),
    )

    assert result.matches[0].match_status == "partial"
    assert result.matches[0].supported_years == expected_years


@pytest.mark.parametrize("name", ["power_bi", "claude", "claude_code"])
def test_adjacent_tools_do_not_prove_specific_requirement(name):
    fact = "Built Tableau dashboards and applied general AI models."
    target = requirement(name)
    result = normalize_one(
        target,
        resume_with(fact),
        assessment_for(target, evidence=[fact]),
    )

    assert result.matches[0].match_status == "missing"
    assert result.matches[0].resume_evidence == []


def test_langgraph_and_custom_handlers_share_match_post_processing():
    fact = "Built Tableau dashboards."
    power_bi = requirement("power_bi")
    job = JobAnalysis(
        title="Analyst",
        summary="Build dashboards",
        requirements=[power_bi],
        responsibilities=[],
    )
    resume = resume_with(fact)
    assessment = assessment_for(power_bi, evidence=[fact])

    with patch("job_agent.nodes._analyze", return_value=assessment):
        langgraph = SkillMatch.model_validate(
            match_skills(
                {"resume_analysis": resume, "job_analysis": job},
                {},
            )["skill_match"]
        )

    handler = JobAgentStepHandler(
        analyzer=lambda schema, system_message, human_message: assessment
    )
    state = AgentState(
        run_id="match-parity",
        resume_text=fact,
        job_description="Power BI required",
        step=Step.MATCH_SKILLS,
        resume_analysis=resume,
        job_analysis=job,
    )
    custom = handler.execute(Step.MATCH_SKILLS, state).updates["skill_match"]

    assert custom == langgraph
    assert custom.matches[0].match_status == "missing"
