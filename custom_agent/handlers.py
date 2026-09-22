"""LangGraph-independent handlers for the Job Agent computation steps."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from custom_agent.state import AgentState, Step
from custom_agent.steps import StepOutcome
from job_agent.domain import (
    normalize_job_analysis,
    normalize_resume_analysis,
    normalize_skill_match,
    normalize_tailored_resume_claim_ids,
    normalize_text,
)
from job_agent.quality import clean_tailored_resume, quality_result, validate_resume_structure
from job_agent.model import DEFAULT_ENV_PATH, create_model, invoke_structured
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
    ResumeAnalysis,
    ResumeEvidence,
    SkillAssessment,
    TailoredResume,
    UnsupportedClaim,
    VerificationResult,
)


class Analyzer(Protocol):
    def __call__(
        self, schema: type[BaseModel], system_message: str, human_message: str
    ) -> BaseModel: ...


class SharedModelAnalyzer:
    def __init__(self, env_path: Path = DEFAULT_ENV_PATH) -> None:
        self._env_path = env_path

    def __call__(
        self, schema: type[BaseModel], system_message: str, human_message: str
    ) -> BaseModel:
        model = create_model(env_path=self._env_path)
        return invoke_structured(model, schema, system_message, human_message)


class JobAgentStepHandler:
    """Execute the same business steps and prompts as the LangGraph nodes."""

    def __init__(self, analyzer: Analyzer | None = None) -> None:
        self._analyze = analyzer or SharedModelAnalyzer()

    def execute(self, step: Step, state: AgentState) -> StepOutcome:
        handlers = {
            Step.VALIDATE_INPUT: self._validate_input,
            Step.ANALYZE_RESUME: self._analyze_resume,
            Step.VALIDATE_EVIDENCE: self._validate_evidence,
            Step.ANALYZE_JOB: self._analyze_job,
            Step.MATCH_SKILLS: self._match_skills,
            Step.WRITE_RESUME: self._write_resume,
            Step.VERIFY_RESUME: self._verify_resume,
            Step.REVISE_RESUME: self._revise_resume,
        }
        handler = handlers.get(step)
        if handler is None:
            raise ValueError(f"Step {step} is not a computation step.")
        return StepOutcome(updates=handler(state))

    @staticmethod
    def _validate_input(state: AgentState) -> dict:
        if not state.resume_text.strip():
            raise ValueError("resume_text must be a non-empty string.")
        if not state.job_description.strip():
            raise ValueError("job_description must be a non-empty string.")
        return {
            "verification": None,
            "revision_feedback": [],
            "revision_count": 0,
            "approved": None,
            "human_feedback": None,
        }

    def _analyze_resume(self, state: AgentState) -> dict:
        analysis = ResumeAnalysis.model_validate(
            self._analyze(ResumeAnalysis, RESUME_PROMPT, state.resume_text.strip())
        )
        return {"resume_analysis": normalize_resume_analysis(analysis)}

    @staticmethod
    def _validate_evidence(state: AgentState) -> dict:
        if state.resume_analysis is None:
            raise ValueError("Resume analysis is required before evidence validation.")
        original = normalize_text(state.resume_text)
        invalid = [
            item.evidence_id
            for item in state.resume_analysis.evidence
            if normalize_text(item.exact_text) not in original
        ]
        if invalid:
            raise ValueError(f"Evidence is not present in the original resume: {invalid}")
        return {}

    def _analyze_job(self, state: AgentState) -> dict:
        analysis = JobAnalysis.model_validate(
            self._analyze(JobAnalysis, JOB_PROMPT, state.job_description.strip())
        )
        return {
            "job_analysis": normalize_job_analysis(
                analysis,
                state.job_description.strip(),
            )
        }

    def _match_skills(self, state: AgentState) -> dict:
        if state.resume_analysis is None or state.job_analysis is None:
            raise ValueError("Resume and job analyses are required before matching skills.")
        resume = state.resume_analysis
        job = state.job_analysis
        content = (
            f"Resume analysis:\n{resume.model_dump_json()}\n\n"
            f"Job analysis:\n{job.model_dump_json()}"
        )
        assessment = SkillAssessment.model_validate(
            self._analyze(SkillAssessment, MATCH_PROMPT, content)
        )
        skill_match = normalize_skill_match(resume, job, assessment)
        return {"skill_match": skill_match}

    def _write_resume(self, state: AgentState) -> dict:
        if (
            state.resume_analysis is None
            or state.job_analysis is None
            or state.skill_match is None
        ):
            raise ValueError("Analyses and skill match are required before writing.")
        content = (
            f"SOURCE OF TRUTH - ORIGINAL RESUME:\n{state.resume_text}\n\n"
            f"GROUNDED RESUME EVIDENCE:\n{state.resume_analysis.model_dump_json()}\n\n"
            f"TARGET REQUIREMENTS - JOB ANALYSIS:\n{state.job_analysis.model_dump_json()}\n\n"
            f"SKILL MATCH:\n{state.skill_match.model_dump_json()}\n\n"
            "STRUCTURAL GUIDANCE:\n"
            "Use at most two summary sentences and at most five selected bullets per source entry. "
            "Return target requirement IDs only from the supplied Job Analysis."
        )
        draft = normalize_tailored_resume_claim_ids(TailoredResume.model_validate(
            self._analyze(TailoredResume, WRITE_RESUME_PROMPT, content)
        ))
        cleaned, _ = clean_tailored_resume(draft)
        return {"tailored_resume": cleaned}

    def _verify_resume(self, state: AgentState) -> dict:
        if state.tailored_resume is None or state.resume_analysis is None:
            raise ValueError("A tailored resume and resume analysis are required.")
        content = (
            f"SOURCE OF TRUTH - ORIGINAL RESUME:\n{state.resume_text}\n\n"
            f"GROUNDED EVIDENCE WITH IDS:\n{state.resume_analysis.model_dump_json()}\n\n"
            "TARGET REQUIREMENTS - JOB DESCRIPTION (NOT EVIDENCE):\n"
            f"{state.job_description}\n\n"
            f"GENERATED CLAIMS TO VERIFY:\n{state.tailored_resume.model_dump_json()}"
        )
        verification = VerificationResult.model_validate(
            self._analyze(VerificationResult, VERIFY_RESUME_PROMPT, content)
        )
        known_ids = {item.evidence_id for item in state.resume_analysis.evidence}
        unsupported = list(verification.unsupported_claims)
        feedback = list(verification.revision_feedback)
        claims = state.tailored_resume.claims()
        for claim in claims:
            if not claim.evidence_ids:
                unsupported.append(
                    UnsupportedClaim(
                        claim=claim.text,
                        reason="The claim does not reference any resume evidence.",
                    )
                )
                feedback.append(
                    f"Remove or rewrite '{claim.text}' with resume evidence IDs."
                )
                continue
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
        quality_issues = validate_resume_structure(
            state.tailored_resume, state.resume_analysis.source_entries,
            known_evidence_ids=known_ids,
        )
        quality = quality_result(quality_issues)
        feedback.extend(quality.revision_feedback)
        if unsupported != verification.unsupported_claims or quality_issues:
            verification = VerificationResult(
                passed=False, unsupported_claims=unsupported,
                revision_feedback=list(dict.fromkeys(feedback)), quality=quality,
            )
        else:
            verification = VerificationResult.model_validate({
                **verification.model_dump(mode="python"), "quality": quality,
            })
        return {
            "verification": verification,
            "revision_feedback": verification.revision_feedback,
        }

    def _revise_resume(self, state: AgentState) -> dict:
        if (
            state.verification is None
            or state.resume_analysis is None
            or state.job_analysis is None
            or state.tailored_resume is None
        ):
            raise ValueError("Complete resume state is required before revision.")
        feedback = list(state.verification.revision_feedback)
        if state.human_feedback:
            feedback.append(state.human_feedback)
        content = (
            f"SOURCE OF TRUTH - ORIGINAL RESUME:\n{state.resume_text}\n\n"
            f"GROUNDED EVIDENCE WITH IDS:\n{state.resume_analysis.model_dump_json()}\n\n"
            f"CURRENT TAILORED RESUME:\n{state.tailored_resume.model_dump_json()}\n\n"
            f"REVISION FEEDBACK:\n{json.dumps(feedback, ensure_ascii=False)}\n\n"
            "TARGET REQUIREMENTS - JOB ANALYSIS (NOT EVIDENCE):\n"
            f"{state.job_analysis.model_dump_json()}"
        )
        revised = normalize_tailored_resume_claim_ids(TailoredResume.model_validate(
            self._analyze(TailoredResume, REVISE_RESUME_PROMPT, content)
        ))
        revised, _ = clean_tailored_resume(revised)
        return {
            "tailored_resume": revised,
            "approved": None,
            "human_feedback": None,
        }
