"""Analysis nodes; the model is initialized only when a node runs."""
import hashlib
import os
import re
from pathlib import Path
from typing import TypeVar

from dotenv import dotenv_values
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from job_agent.prompts import (
    JOB_PROMPT,
    MATCH_PROMPT,
    RESUME_PROMPT,
    REVISE_RESUME_PROMPT,
    VERIFY_RESUME_PROMPT,
    WRITE_RESUME_PROMPT,
)
from job_agent.schemas import (
    JobAnalysis,
    JobRequirement,
    ResumeAnalysis,
    ResumeEvidence,
    SkillAssessment,
    SkillEvidence,
    SkillMatch,
    TailoredResume,
    UnsupportedClaim,
    VerificationResult,
)
from job_agent.state import JobAgentState

Result = TypeVar("Result", bound=BaseModel)
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _create_model() -> ChatOpenAI:
    # Read the project's .env on each call so edits take effect without restarting.
    settings = {**dotenv_values(ENV_PATH), **os.environ}
    model_id = (settings.get("LLM_MODEL_ID") or "").strip()
    if not model_id:
        raise ValueError("Set LLM_MODEL_ID in the environment or project's .env file.")
    options = {"model": model_id}
    api_key = settings.get("LLM_API_KEY")
    base_url = settings.get("LLM_BASE_URL")
    timeout = settings.get("LLM_TIMEOUT")
    if api_key:
        options["api_key"] = api_key
    if base_url:
        options["base_url"] = base_url
    if timeout:
        try:
            seconds = float(timeout)
        except ValueError as exc:
            raise ValueError("LLM_TIMEOUT must be a positive number of seconds.") from exc
        if not 0 < seconds < float("inf"):
            raise ValueError("LLM_TIMEOUT must be a positive number of seconds.")
        options["timeout"] = seconds
    return ChatOpenAI(**options)


def _analyze(schema: type[Result], prompt: str, content: str,
             config: RunnableConfig | None = None) -> Result:
    model = _create_model()
    structured_model = model.with_structured_output(schema)
    result = structured_model.invoke(
        [("system", prompt), ("human", content)], config=config
    )
    if isinstance(result, schema):
        return result
    return schema.model_validate(result)


def _require_text(state: JobAgentState, key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string.")
    return value.strip()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def make_evidence_id(text: str) -> str:
    digest = hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()[:8]
    return f"EXP-{digest}"


def validate_input(state: JobAgentState) -> dict:
    """Reject invalid workflow inputs before the first paid model call."""
    _require_text(state, "resume_text")
    _require_text(state, "job_description")
    max_revisions = state.get("max_revisions", 3)
    if isinstance(max_revisions, bool) or not isinstance(max_revisions, int):
        raise ValueError("max_revisions must be an integer from 0 to 3.")
    if not 0 <= max_revisions <= 3:
        raise ValueError("max_revisions must be an integer from 0 to 3.")
    return {
        "verification": None,
        "revision_feedback": [],
        "revision_count": 0,
        "max_revisions": max_revisions,
    }


def analyze_resume(state: JobAgentState, config: RunnableConfig) -> dict:
    resume = state["resume_text"].strip()
    analysis = _analyze(ResumeAnalysis, RESUME_PROMPT, resume, config)
    evidence_by_text = {}
    for item in analysis.evidence:
        exact_text = item.exact_text.strip()
        normalized = normalize_text(exact_text)
        if normalized and normalized not in evidence_by_text:
            evidence_by_text[normalized] = ResumeEvidence(
                evidence_id=make_evidence_id(exact_text),
                source_section=item.source_section,
                exact_text=exact_text,
            )
    return {
        "resume_analysis": analysis.model_copy(
            update={"evidence": list(evidence_by_text.values())}
        )
    }


def validate_extracted_evidence(state: JobAgentState) -> dict:
    original = normalize_text(state["resume_text"])
    analysis = ResumeAnalysis.model_validate(state["resume_analysis"])
    invalid_evidence = [
        item.evidence_id
        for item in analysis.evidence
        if normalize_text(item.exact_text) not in original
    ]
    if invalid_evidence:
        raise ValueError(
            f"Evidence is not present in the original resume: {invalid_evidence}"
        )
    return {}


def analyze_job(state: JobAgentState, config: RunnableConfig) -> dict:
    job = state["job_description"].strip()
    analysis = _analyze(JobAnalysis, JOB_PROMPT, job, config)
    requirements_by_name = {}
    for item in analysis.requirements:
        canonical_name = normalize_text(item.canonical_name)
        if not canonical_name:
            continue
        normalized_item = item.model_copy(update={"canonical_name": canonical_name})
        existing = requirements_by_name.get(canonical_name)
        if existing is None or (
            existing.level == "preferred" and normalized_item.level == "required"
        ):
            requirements_by_name[canonical_name] = normalized_item
    requirements = [
        JobRequirement(
            requirement_id=f"REQ-{index:03d}",
            canonical_name=item.canonical_name,
            original_text=item.original_text,
            level=item.level,
        )
        for index, item in enumerate(requirements_by_name.values(), start=1)
    ]
    return {
        "job_analysis": analysis.model_copy(update={"requirements": requirements})
    }


def match_skills(state: JobAgentState, config: RunnableConfig) -> dict:
    if "resume_analysis" not in state or "job_analysis" not in state:
        raise ValueError("Resume and job analyses are required before matching skills.")
    resume = ResumeAnalysis.model_validate(state["resume_analysis"])
    job = JobAnalysis.model_validate(state["job_analysis"])
    content = (
        f"Resume analysis:\n{resume.model_dump_json()}\n\n"
        f"Job analysis:\n{job.model_dump_json()}"
    )
    assessment = _analyze(SkillAssessment, MATCH_PROMPT, content, config)
    allowed_evidence = {item.exact_text for item in resume.evidence}
    supplied_by_id = {item.requirement_id: item for item in assessment.matches}
    supplied_by_name = {
        normalize_text(item.job_skill): item for item in assessment.matches
    }
    matches = []
    for requirement in job.requirements:
        item = supplied_by_id.get(requirement.requirement_id)
        if item is None:
            item = supplied_by_name.get(normalize_text(requirement.canonical_name))
        if item is None:
            item = SkillEvidence(
                requirement_id=requirement.requirement_id,
                job_skill=requirement.canonical_name,
                requirement_level=requirement.level,
                match_status="missing",
                resume_evidence=[],
                confidence=1,
            )
        valid_evidence = [
            evidence for evidence in item.resume_evidence if evidence in allowed_evidence
        ]
        status = item.match_status
        confidence = item.confidence
        if status != "missing" and not valid_evidence:
            status = "missing"
            confidence = 0
        matches.append(
            item.model_copy(
                update={
                    "requirement_id": requirement.requirement_id,
                    "job_skill": requirement.canonical_name,
                    "requirement_level": requirement.level,
                    "match_status": status,
                    "resume_evidence": valid_evidence,
                    "confidence": confidence,
                }
            )
        )
    missing_required = [
        item.job_skill for item in matches
        if item.requirement_level == "required" and item.match_status != "matched"
    ]
    missing_preferred = [
        item.job_skill for item in matches
        if item.requirement_level == "preferred" and item.match_status != "matched"
    ]
    return {
        "skill_match": SkillMatch(
            **assessment.model_dump(exclude={"matches"}),
            matches=matches,
            missing_required_skills=missing_required,
            missing_preferred_skills=missing_preferred,
            overall_score=calculate_match_score(matches),
        )
    }


def calculate_match_score(matches: list[SkillEvidence]) -> float:
    weights = {"required": 2.0, "preferred": 1.0}
    values = {"matched": 1.0, "partial": 0.5, "missing": 0.0}
    possible = sum(weights[item.requirement_level] for item in matches)
    if not possible:
        return 0.0
    earned = sum(
        weights[item.requirement_level] * values[item.match_status]
        for item in matches
    )
    return round(earned / possible * 100, 1)


def write_resume(state: JobAgentState, config: RunnableConfig) -> dict:
    if "job_analysis" not in state or "skill_match" not in state:
        raise ValueError("Job analysis and skill match are required before writing.")
    content = (
        f"SOURCE OF TRUTH - ORIGINAL RESUME:\n{state['resume_text']}\n\n"
        f"GROUNDED RESUME EVIDENCE:\n"
        f"{ResumeAnalysis.model_validate(state['resume_analysis']).model_dump_json()}\n\n"
        f"TARGET REQUIREMENTS - JOB ANALYSIS:\n"
        f"{JobAnalysis.model_validate(state['job_analysis']).model_dump_json()}\n\n"
        f"SKILL MATCH:\n{SkillMatch.model_validate(state['skill_match']).model_dump_json()}"
    )
    draft = _analyze(TailoredResume, WRITE_RESUME_PROMPT, content, config)
    return {"tailored_resume": draft}


def verify_resume(state: JobAgentState, config: RunnableConfig) -> dict:
    tailored_resume = state.get("tailored_resume")
    if tailored_resume is None:
        raise ValueError("A tailored resume is required before verification.")
    tailored_resume = TailoredResume.model_validate(tailored_resume)
    resume_analysis = ResumeAnalysis.model_validate(state["resume_analysis"])
    content = (
        f"SOURCE OF TRUTH - ORIGINAL RESUME:\n{state['resume_text']}\n\n"
        f"GROUNDED EVIDENCE WITH IDS:\n{resume_analysis.model_dump_json()}\n\n"
        f"TARGET REQUIREMENTS - JOB DESCRIPTION (NOT EVIDENCE):\n"
        f"{state['job_description']}\n\n"
        f"GENERATED CLAIMS TO VERIFY:\n{tailored_resume.model_dump_json()}"
    )
    verification = _analyze(
        VerificationResult, VERIFY_RESUME_PROMPT, content, config
    )
    known_ids = {item.evidence_id for item in resume_analysis.evidence}
    unsupported = list(verification.unsupported_claims)
    feedback = list(verification.revision_feedback)
    claims = (
        tailored_resume.professional_summary
        + tailored_resume.experience_bullets
        + tailored_resume.highlighted_skills
    )
    for claim in claims:
        unknown_ids = [item for item in claim.evidence_ids if item not in known_ids]
        if unknown_ids:
            unsupported.append(
                UnsupportedClaim(
                    claim=claim.text,
                    reason=f"References unknown evidence IDs: {', '.join(unknown_ids)}.",
                )
            )
            feedback.append(
                f"Remove or rewrite '{claim.text}' using valid evidence IDs."
            )
    if unsupported != verification.unsupported_claims:
        verification = VerificationResult(
            passed=False,
            unsupported_claims=unsupported,
            revision_feedback=feedback,
        )
    return {
        "verification": verification,
        "revision_feedback": verification.revision_feedback,
    }


def revise_resume(state: JobAgentState, config: RunnableConfig) -> dict:
    verification = state.get("verification")
    if verification is None:
        raise ValueError("Verification result is required before revision.")
    verification = VerificationResult.model_validate(verification)
    content = (
        f"SOURCE OF TRUTH - ORIGINAL RESUME:\n{state['resume_text']}\n\n"
        f"GROUNDED EVIDENCE WITH IDS:\n"
        f"{ResumeAnalysis.model_validate(state['resume_analysis']).model_dump_json()}\n\n"
        f"CURRENT TAILORED RESUME:\n"
        f"{TailoredResume.model_validate(state['tailored_resume']).model_dump_json()}\n\n"
        f"VERIFICATION FEEDBACK:\n{verification.model_dump_json()}\n\n"
        f"TARGET REQUIREMENTS - JOB ANALYSIS (NOT EVIDENCE):\n"
        f"{JobAnalysis.model_validate(state['job_analysis']).model_dump_json()}"
    )
    revised = _analyze(TailoredResume, REVISE_RESUME_PROMPT, content, config)
    return {
        "tailored_resume": revised,
        "revision_count": state["revision_count"] + 1,
    }
