import pytest

from job_agent.schemas import (
    JobAnalysis,
    JobRequirement,
    RequirementCategory,
    ScoreBreakdown,
    SkillEvidence,
    SkillMatch,
    VerificationMode,
)


OLD_JOB_REQUIREMENT = {
    "requirement_id": "REQ-001",
    "canonical_name": "leadership_experience",
    "original_text": "At least five years of leadership experience",
    "level": "required",
    "minimum_years": 5,
}


def test_old_job_requirement_json_populates_v3_compatibility_fields():
    requirement = JobRequirement.model_validate(OLD_JOB_REQUIREMENT)

    assert requirement.requirement_group_id == "REQ-001"
    assert requirement.display_name == "leadership_experience"
    assert requirement.source_text == OLD_JOB_REQUIREMENT["original_text"]
    assert requirement.atomic_text == OLD_JOB_REQUIREMENT["original_text"]
    assert requirement.category == RequirementCategory.SKILL
    assert requirement.verification_mode == VerificationMode.YEARS_EXPERIENCE


def test_old_job_analysis_json_validates_nested_requirements():
    analysis = JobAnalysis.model_validate(
        {
            "title": "Engineering Manager",
            "summary": "Lead an engineering team.",
            "requirements": [OLD_JOB_REQUIREMENT],
            "responsibilities": [],
        }
    )

    assert analysis.requirements[0].requirement_group_id == "REQ-001"
    assert analysis.requirements[0].verification_mode == "years_experience"


def test_explicit_v3_job_requirement_fields_are_preserved():
    requirement = JobRequirement.model_validate(
        {
            **OLD_JOB_REQUIREMENT,
            "requirement_group_id": "GROUP-LEADERSHIP",
            "display_name": "Engineering leadership",
            "source_text": "Leadership and mentoring experience required",
            "atomic_text": "Leadership experience required",
            "category": "experience",
            "verification_mode": "user_confirmation",
        }
    )

    assert requirement.requirement_group_id == "GROUP-LEADERSHIP"
    assert requirement.display_name == "Engineering leadership"
    assert requirement.category == RequirementCategory.EXPERIENCE
    assert requirement.verification_mode == VerificationMode.USER_CONFIRMATION


def test_old_skill_evidence_json_retains_safe_defaults():
    match = SkillEvidence.model_validate(
        {
            "requirement_id": "REQ-001",
            "job_skill": "leadership_experience",
            "requirement_level": "required",
            "match_status": "partial",
            "resume_evidence": ["Mentored two junior developers."],
            "confidence": 0.7,
        }
    )

    assert match.match_reason is None
    assert match.supported_years is None


def test_needs_confirmation_match_status_and_years_are_supported():
    match = SkillEvidence(
        requirement_id="REQ-002",
        job_skill="work_authorization",
        requirement_level="required",
        match_status="needs_confirmation",
        match_reason="The resume does not state work authorization.",
        supported_years=2.5,
        resume_evidence=[],
        confidence=1,
    )

    assert match.match_status == "needs_confirmation"
    assert match.supported_years == 2.5
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        SkillEvidence.model_validate({**match.model_dump(), "supported_years": -1})


def test_old_skill_match_json_populates_score_and_confirmation_defaults():
    skill_match = SkillMatch.model_validate(
        {
            "matches": [],
            "explanation": "No requirements were supplied.",
            "recommendations": [],
            "missing_required_requirements": [],
            "missing_preferred_requirements": [],
            "overall_score": 82.5,
        }
    )

    assert skill_match.score_breakdown == ScoreBreakdown(overall_score=82.5)
    assert skill_match.confirmation_requirements == []


def test_confirmation_requirements_use_full_requirement_contract():
    skill_match = SkillMatch.model_validate(
        {
            "matches": [],
            "explanation": "Authorization needs confirmation.",
            "recommendations": [],
            "missing_required_requirements": [],
            "missing_preferred_requirements": [],
            "overall_score": 100,
            "score_breakdown": {
                "required_score": 100,
                "preferred_score": None,
                "overall_score": 100,
            },
            "confirmation_requirements": [
                {
                    **OLD_JOB_REQUIREMENT,
                    "canonical_name": "work_authorization",
                    "original_text": "Must be authorized to work locally",
                    "minimum_years": None,
                    "category": "eligibility",
                    "verification_mode": "user_confirmation",
                }
            ],
        }
    )

    confirmation = skill_match.confirmation_requirements[0]
    assert confirmation.category == RequirementCategory.ELIGIBILITY
    assert confirmation.verification_mode == VerificationMode.USER_CONFIRMATION
