from unittest.mock import patch

from job_agent.domain import calculate_score_breakdown
from job_agent.nodes import match_skills
from job_agent.schemas import (
    JobAnalysis,
    JobRequirement,
    ResumeAnalysis,
    ResumeEvidence,
    ScoreBreakdown,
    SkillAssessment,
    SkillEvidence,
    SkillMatch,
)


def requirement(
    requirement_id: str,
    group_id: str,
    *,
    level: str = "required",
) -> JobRequirement:
    return JobRequirement(
        requirement_id=requirement_id,
        requirement_group_id=group_id,
        canonical_name=requirement_id.lower(),
        display_name=requirement_id,
        original_text=requirement_id,
        source_text=requirement_id,
        atomic_text=requirement_id,
        category="skill",
        verification_mode="resume_evidence",
        level=level,
    )


def match(
    requirement_: JobRequirement,
    status: str,
    *,
    evidence: list[str] | None = None,
) -> SkillEvidence:
    return SkillEvidence(
        requirement_id=requirement_.requirement_id,
        job_skill=requirement_.canonical_name,
        requirement_level=requirement_.level,
        match_status=status,
        resume_evidence=evidence or [],
        confidence=1,
    )


def test_splitting_compound_requirement_does_not_increase_group_weight():
    other = requirement("OTHER", "GROUP-OTHER")
    unsplit = requirement("COMPOUND", "GROUP-COMPOUND")
    split_a = requirement("SPLIT-A", "GROUP-COMPOUND")
    split_b = requirement("SPLIT-B", "GROUP-COMPOUND")

    unsplit_result = calculate_score_breakdown(
        [match(unsplit, "matched"), match(other, "missing")],
        [unsplit, other],
    )
    split_result = calculate_score_breakdown(
        [
            match(split_a, "matched"),
            match(split_b, "matched"),
            match(other, "missing"),
        ],
        [split_a, split_b, other],
    )

    assert unsplit_result.overall_score == 50.0
    assert split_result.overall_score == unsplit_result.overall_score
    assert split_result.required_score == 50.0


def test_atomic_results_are_averaged_before_group_weights_are_applied():
    required_a = requirement("REQUIRED-A", "GROUP-REQUIRED")
    required_b = requirement("REQUIRED-B", "GROUP-REQUIRED")
    preferred = requirement("PREFERRED", "GROUP-PREFERRED", level="preferred")

    result = calculate_score_breakdown(
        [
            match(required_a, "matched"),
            match(required_b, "missing"),
            match(preferred, "matched"),
        ],
        [required_a, required_b, preferred],
    )

    assert result == ScoreBreakdown(
        required_score=50.0,
        preferred_score=100.0,
        overall_score=66.7,
    )


def test_confirmation_groups_are_excluded_from_score_breakdown():
    confirmation = requirement("AUTHORIZATION", "GROUP-AUTHORIZATION")
    preferred = requirement("PYTHON", "GROUP-PYTHON", level="preferred")

    result = calculate_score_breakdown(
        [
            match(confirmation, "needs_confirmation"),
            match(preferred, "matched"),
        ],
        [confirmation, preferred],
    )

    assert result == ScoreBreakdown(
        required_score=None,
        preferred_score=100.0,
        overall_score=100.0,
    )


def test_match_handler_returns_grouped_score_breakdown():
    fact = "Built Python APIs."
    python = requirement("PYTHON", "GROUP-COMPOUND")
    sql = requirement("SQL", "GROUP-COMPOUND")
    resume = ResumeAnalysis(
        summary="Developer",
        skills=["Python"],
        evidence=[
            ResumeEvidence(
                evidence_id="EXP-1",
                source_section="Experience",
                exact_text=fact,
            )
        ],
        education=[],
    )
    job = JobAnalysis(
        title="Developer",
        summary="Build services",
        requirements=[python, sql],
        responsibilities=[],
    )
    assessment = SkillAssessment(
        matches=[
            match(python, "matched", evidence=[fact]),
            match(sql, "missing"),
        ],
        explanation="Python is supported and SQL is missing.",
        recommendations=[],
    )

    with patch("job_agent.nodes._analyze", return_value=assessment):
        result = SkillMatch.model_validate(
            match_skills(
                {"resume_analysis": resume, "job_analysis": job},
                {},
            )["skill_match"]
        )

    assert result.overall_score == 50.0
    assert result.score_breakdown == ScoreBreakdown(
        required_score=50.0,
        preferred_score=None,
        overall_score=50.0,
    )
