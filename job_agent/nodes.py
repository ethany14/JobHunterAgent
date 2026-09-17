"""Analysis nodes; the model is initialized only when a node runs."""
import json
from pathlib import Path
from typing import TypeVar

from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.types import interrupt
from pydantic import BaseModel

from job_agent.prompts import (
    JOB_PROMPT,
    MATCH_PROMPT,
    RESUME_PROMPT,
    REVISE_RESUME_PROMPT,
    VERIFY_RESUME_PROMPT,
    WRITE_RESUME_PROMPT,
)
from job_agent.model import create_model, invoke_structured
from job_agent.domain import (
    calculate_match_score,
    canonicalize_requirement,
    extract_minimum_years,
    make_evidence_id,
    normalize_job_analysis,
    normalize_skill_match,
    normalize_text,
)
from job_agent.schemas import (
    JobAnalysis,
    ResumeAnalysis,
    ResumeEvidence,
    SkillAssessment,
    SkillMatch,
    TailoredResume,
    UnsupportedClaim,
    VerificationResult,
)
from job_agent.state import JobAgentState

Result = TypeVar("Result", bound=BaseModel)
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _create_model() -> ChatOpenAI:
    # Preserve the patch point used by baseline tests while sharing configuration.
    return create_model(env_path=ENV_PATH, model_factory=ChatOpenAI)


def _analyze(schema: type[Result], prompt: str, content: str,
             config: RunnableConfig | None = None) -> Result:
    return invoke_structured(_create_model(), schema, prompt, content, config)


def _require_text(state: JobAgentState, key: str) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string.")
    return value.strip()


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
        "approved": None,
        "human_feedback": None,
        "workflow_status": "running",
    }


def analyze_resume(state: JobAgentState, config: RunnableConfig) -> dict:
    resume = state["resume_text"].strip()
    analysis = _analyze(ResumeAnalysis, RESUME_PROMPT, resume, config)
    evidence_by_text = {}
    for item in analysis.evidence:
        exact_text = item.exact_text
        normalized = normalize_text(exact_text)
        if normalized and normalized not in evidence_by_text:
            evidence_by_text[normalized] = ResumeEvidence(
                evidence_id=make_evidence_id(exact_text),
                source_section=item.source_section,
                exact_text=exact_text,
            )
    grounded = analysis.model_copy(
        update={"evidence": list(evidence_by_text.values())}
    )
    return {"resume_analysis": grounded.model_dump(mode="json")}


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
    return {"job_analysis": normalize_job_analysis(analysis, job).model_dump(mode="json")}


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
    skill_match = normalize_skill_match(resume, job, assessment)
    return {
        "skill_match": skill_match.model_dump(mode="json")
    }


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
    return {"tailored_resume": draft.model_dump(mode="json")}


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
        "verification": verification.model_dump(mode="json"),
        "revision_feedback": verification.revision_feedback,
    }


def revise_resume(state: JobAgentState, config: RunnableConfig) -> dict:
    verification = state.get("verification")
    if verification is None:
        raise ValueError("Verification result is required before revision.")
    verification = VerificationResult.model_validate(verification)
    feedback = list(verification.revision_feedback)
    if state.get("human_feedback"):
        feedback.append(state["human_feedback"])
    content = (
        f"SOURCE OF TRUTH - ORIGINAL RESUME:\n{state['resume_text']}\n\n"
        f"GROUNDED EVIDENCE WITH IDS:\n"
        f"{ResumeAnalysis.model_validate(state['resume_analysis']).model_dump_json()}\n\n"
        f"CURRENT TAILORED RESUME:\n"
        f"{TailoredResume.model_validate(state['tailored_resume']).model_dump_json()}\n\n"
        f"REVISION FEEDBACK:\n{json.dumps(feedback, ensure_ascii=False)}\n\n"
        f"TARGET REQUIREMENTS - JOB ANALYSIS (NOT EVIDENCE):\n"
        f"{JobAnalysis.model_validate(state['job_analysis']).model_dump_json()}"
    )
    revised = _analyze(TailoredResume, REVISE_RESUME_PROMPT, content, config)
    return {
        "tailored_resume": revised.model_dump(mode="json"),
        "revision_count": state["revision_count"] + 1,
        "approved": None,
        "human_feedback": None,
        "workflow_status": "running",
    }


def human_review(state: JobAgentState) -> dict:
    tailored_resume = TailoredResume.model_validate(state["tailored_resume"])
    verification = VerificationResult.model_validate(state["verification"])
    decision = interrupt(
        {
            "question": "Do you approve this tailored resume?",
            "tailored_resume": tailored_resume.model_dump(mode="json"),
            "verification": verification.model_dump(mode="json"),
            "revision_count": state["revision_count"],
        }
    )
    if not isinstance(decision, dict):
        raise ValueError("Human review decision must be an object.")
    approved = bool(decision.get("approved"))
    feedback = decision.get("feedback")
    if feedback is not None and not isinstance(feedback, str):
        raise ValueError("Human feedback must be a string or null.")
    if isinstance(feedback, str):
        feedback = feedback.strip() or None
    if not approved and not feedback:
        raise ValueError("Feedback is required when the resume is rejected.")
    return {
        "approved": approved,
        "human_feedback": feedback,
        "workflow_status": "approved" if approved else "revision_requested",
    }
