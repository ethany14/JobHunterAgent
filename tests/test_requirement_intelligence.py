from unittest.mock import patch

import pytest

from custom_agent.handlers import JobAgentStepHandler
from custom_agent.state import AgentState, Step
from job_agent.domain import (
    make_requirement_group_id,
    make_requirement_id,
    normalize_job_analysis,
)
from job_agent.nodes import analyze_job
from job_agent.prompts import JOB_PROMPT
from job_agent.schemas import (
    JobAnalysis,
    JobRequirement,
    RequirementCategory,
    VerificationMode,
)


PYTHON_SQL_SOURCE = "Required: Python and SQL."
ELIGIBILITY_SOURCE = "Applicants must be authorized to work in Australia."
JOB_DESCRIPTION = f"{PYTHON_SQL_SOURCE} {ELIGIBILITY_SOURCE} Python preferred."


def requirement(
    *,
    requirement_id: str,
    canonical_name: str,
    display_name: str,
    source_text: str,
    atomic_text: str,
    level: str = "required",
    category: str = "skill",
    verification_mode: str = "resume_evidence",
    minimum_years: int | None = None,
) -> JobRequirement:
    return JobRequirement(
        requirement_id=requirement_id,
        requirement_group_id="temporary-group",
        canonical_name=canonical_name,
        display_name=display_name,
        original_text=atomic_text,
        source_text=source_text,
        atomic_text=atomic_text,
        category=category,
        verification_mode=verification_mode,
        level=level,
        minimum_years=minimum_years,
    )


def extracted_analysis() -> JobAnalysis:
    return JobAnalysis(
        title="Backend Engineer",
        summary="Build services.",
        requirements=[
            requirement(
                requirement_id="temp-python-required",
                canonical_name="Python programming",
                display_name="Python",
                source_text=PYTHON_SQL_SOURCE,
                atomic_text="Python",
            ),
            requirement(
                requirement_id="temp-sql",
                canonical_name="SQL",
                display_name="SQL",
                source_text=PYTHON_SQL_SOURCE,
                atomic_text="SQL",
            ),
            requirement(
                requirement_id="temp-eligibility",
                canonical_name="work_authorization",
                display_name="Australian work authorization",
                source_text=ELIGIBILITY_SOURCE,
                atomic_text="authorized to work in Australia",
                category="other",
            ),
            requirement(
                requirement_id="temp-python-preferred",
                canonical_name="python",
                display_name="Python",
                source_text="Python preferred.",
                atomic_text="Python",
                level="preferred",
            ),
        ],
        responsibilities=[],
    )


def test_job_prompt_requests_v3_requirement_intelligence_fields():
    normalized = " ".join(JOB_PROMPT.split())

    for field in (
        "atomic_text",
        "requirement_group_id",
        "source_text",
        "display_name",
        "category",
        "verification_mode",
        "minimum_years",
    ):
        assert field in normalized
    assert "verbatim" in normalized
    assert "user_confirmation" in normalized


def test_normalization_rejects_source_text_absent_from_original_jd():
    analysis = extracted_analysis()
    analysis.requirements[0] = requirement(
        requirement_id="invented",
        canonical_name="kubernetes",
        display_name="Kubernetes",
        source_text="Kubernetes is required.",
        atomic_text="Kubernetes",
    )

    with pytest.raises(ValueError, match="source_text is not present"):
        normalize_job_analysis(analysis, JOB_DESCRIPTION)


def test_normalization_generates_stable_group_and_requirement_ids():
    first = normalize_job_analysis(extracted_analysis(), JOB_DESCRIPTION)
    second = normalize_job_analysis(extracted_analysis(), JOB_DESCRIPTION)
    by_name = {item.canonical_name: item for item in first.requirements}

    assert first == second
    assert by_name["python"].requirement_group_id == by_name["sql"].requirement_group_id
    assert by_name["python"].requirement_id != by_name["sql"].requirement_id
    expected_group = make_requirement_group_id(PYTHON_SQL_SOURCE)
    assert by_name["python"].requirement_group_id == expected_group
    assert by_name["python"].requirement_id == make_requirement_id(
        expected_group, "Python"
    )


def test_normalization_forces_eligibility_to_user_confirmation():
    normalized = normalize_job_analysis(extracted_analysis(), JOB_DESCRIPTION)
    eligibility = next(
        item for item in normalized.requirements
        if item.canonical_name == "work_authorization"
    )

    assert eligibility.category == RequirementCategory.OTHER
    assert eligibility.verification_mode == VerificationMode.USER_CONFIRMATION


def test_normalization_extracts_only_explicit_minimum_years():
    source = "Requires at least five years of engineering leadership experience."
    analysis = JobAnalysis(
        title="Engineering Manager",
        summary="Lead an engineering team.",
        requirements=[
            requirement(
                requirement_id="temporary",
                canonical_name="leadership",
                display_name="Engineering leadership",
                source_text=source,
                atomic_text="at least five years of engineering leadership experience",
                category="experience",
                verification_mode="years_experience",
            )
        ],
        responsibilities=[],
    )

    normalized = normalize_job_analysis(analysis, source)

    assert normalized.requirements[0].minimum_years == 5
    assert normalized.requirements[0].verification_mode == VerificationMode.YEARS_EXPERIENCE


def test_normalization_keeps_required_over_preferred_duplicate():
    normalized = normalize_job_analysis(extracted_analysis(), JOB_DESCRIPTION)
    python_requirements = [
        item for item in normalized.requirements if item.canonical_name == "python"
    ]

    assert len(python_requirements) == 1
    assert python_requirements[0].level == "required"
    assert python_requirements[0].source_text == PYTHON_SQL_SOURCE


def test_langgraph_and_custom_backend_share_identical_job_normalization():
    raw = extracted_analysis()
    with patch("job_agent.nodes._analyze", return_value=raw):
        langgraph_result = JobAnalysis.model_validate(
            analyze_job({"job_description": JOB_DESCRIPTION}, {})["job_analysis"]
        )

    handler = JobAgentStepHandler(
        analyzer=lambda schema, system_message, human_message: raw
    )
    state = AgentState(
        run_id="requirement-parity",
        resume_text="Resume",
        job_description=JOB_DESCRIPTION,
        step=Step.ANALYZE_JOB,
    )
    custom_result = handler.execute(Step.ANALYZE_JOB, state).updates["job_analysis"]

    assert custom_result == langgraph_result
