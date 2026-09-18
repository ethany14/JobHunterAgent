import json
from pathlib import Path

from evals.run_requirement_intelligence_v3 import evaluate_outputs, semantic_key
from job_agent.domain import calculate_score_breakdown
from job_agent.schemas import (
    JobAnalysis,
    JobRequirement,
    SkillEvidence,
    SkillMatch,
    TailoredResume,
)


ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "evals" / "requirement_intelligence_v3_cases.json"


def make_requirement(
    key: str,
    category: str,
    *,
    group: str | None = None,
    minimum_years: int | None = None,
    verification_mode: str = "resume_evidence",
) -> JobRequirement:
    atomic = {
        "bachelors_degree": "Bachelor's degree",
        "masters_degree": "Master's degree",
        "consulting_experience": "3+ years experience in a consulting firm",
        "it_project_management_experience": "2+ years IT project management experience",
        "claude": "Claude",
        "claude_code": "Claude Code",
        "cowork": "Cowork",
        "power_bi": "Power BI",
        "certified_scrum_master": "Certified Scrum Master",
        "itil_foundation": "ITIL Foundation",
        "travel": "Ability to travel 50%",
        "work_authorization": "Must be legally authorized to work",
    }[key]
    return JobRequirement(
        requirement_id=f"REQ-{key}",
        requirement_group_id=group or f"REQG-{key}",
        canonical_name=key,
        display_name=atomic,
        original_text=atomic,
        source_text=atomic,
        atomic_text=atomic,
        category=category,
        verification_mode=verification_mode,
        level="required" if key != "masters_degree" else "preferred",
        minimum_years=minimum_years,
    )


def test_deloitte_regression_dataset_is_versioned_and_contains_no_personal_data():
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))

    assert len(cases) == 1
    case = cases[0]
    assert case["id"] == "deloitte_ai_consulting_regression"
    assert "Deloitte" not in case["resume_text"]
    assert "@" not in case["resume_text"]
    assert len(case["expected_duration_decisions"]) == 2
    assert len(case["expected_groups"]) == 2


def test_semantic_key_distinguishes_adjacent_deloitte_requirements():
    claude = make_requirement("claude", "skill", group="REQG-ai-tools")
    claude_code = make_requirement("claude_code", "skill", group="REQG-ai-tools")
    power_bi = make_requirement("power_bi", "skill")

    assert semantic_key(claude) == "claude"
    assert semantic_key(claude_code) == "claude_code"
    assert semantic_key(power_bi) == "power_bi"


def test_deterministic_v3_graders_measure_all_requested_dimensions():
    case = json.loads(CASES_PATH.read_text(encoding="utf-8"))[0]
    group_by_key = {
        "claude": "REQG-ai-tools",
        "claude_code": "REQG-ai-tools",
        "cowork": "REQG-ai-tools",
        "certified_scrum_master": "REQG-certifications",
        "itil_foundation": "REQG-certifications",
    }
    requirements = []
    for key, category in case["expected_categories"].items():
        requirements.append(
            make_requirement(
                key,
                category,
                group=group_by_key.get(key),
                minimum_years=(
                    3
                    if key == "consulting_experience"
                    else 2 if key == "it_project_management_experience" else None
                ),
                verification_mode=(
                    "user_confirmation"
                    if key in case["expected_eligibility"]
                    else "years_experience"
                    if key
                    in {"consulting_experience", "it_project_management_experience"}
                    else "resume_evidence"
                ),
            )
        )

    expected_missing = set(case["expected_missing_canonical"])
    matches = []
    for requirement in requirements:
        key = semantic_key(requirement)
        if key in case["expected_eligibility"]:
            status = "needs_confirmation"
        elif key == "it_project_management_experience":
            status = "matched"
        elif key in expected_missing:
            status = "partial" if key in {
                "bachelors_degree",
                "consulting_experience",
            } else "missing"
        else:
            status = "missing"
        matches.append(
            SkillEvidence(
                requirement_id=requirement.requirement_id,
                job_skill=requirement.canonical_name,
                requirement_level=requirement.level,
                match_status=status,
                resume_evidence=(
                    [requirement.atomic_text]
                    if status in {"matched", "partial"}
                    else []
                ),
                confidence=1,
            )
        )

    job = JobAnalysis(
        title="AI Engineering Consultant",
        summary="Consulting role",
        requirements=requirements,
        responsibilities=[],
    )
    breakdown = calculate_score_breakdown(matches, requirements)
    skill_match = SkillMatch(
        matches=matches,
        explanation="Synthetic grader fixture",
        recommendations=[],
        missing_required_requirements=[],
        missing_preferred_requirements=[],
        confirmation_requirements=[
            item
            for item in requirements
            if item.verification_mode == "user_confirmation"
        ],
        overall_score=breakdown.overall_score,
        score_breakdown=breakdown,
    )
    metrics = evaluate_outputs(
        case,
        job=job,
        skill_match=skill_match,
        tailored_resume=TailoredResume(
            professional_summary=[],
            experience_bullets=[],
            highlighted_skills=[],
        ),
    )

    assert metrics["requirement_category_accuracy"] == 1.0
    assert metrics["eligibility_inference_violations"] == 0
    assert metrics["duration_decision_accuracy"] == 1.0
    assert metrics["in_progress_education_accuracy"] == 1.0
    assert metrics["group_accuracy"] == 1.0
    assert metrics["grouped_score_invariance"] is True
    assert metrics["canonical_missing_recall"] == 1.0
    assert metrics["unsupported_claim_count"] == 0
