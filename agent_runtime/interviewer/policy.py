"""Deterministic requirement ranking and claim validation."""
from __future__ import annotations

import re

from agent_runtime.interviewer.errors import InterviewValidationError
from agent_runtime.interviewer.types import (
    ApplicationRequirementAssessment, EvidenceAssessmentStatus,
    InterviewAnswerAssessment,
)
from job_agent.domain import normalize_text

_EXCLUDED_TARGETS = re.compile(
    r"\b(age|race|religion|disability|gender|marital status|pregnancy|ethnicity)\b",
    re.IGNORECASE,
)
_HIGH_IMPACT = frozenset({"python", "sql", "aws", "leadership", "kubernetes", "ai", "data"})
_INSTRUCTION_IN_ANSWER = re.compile(
    r"\b(ignore (?:your|all|previous) instructions|pretend (?:that )?|claim i |"
    r"system prompt|developer message)\b", re.IGNORECASE,
)


class InterviewPriorityPolicy:
    """Pure stable ordering; no model-generated interview plan."""

    @staticmethod
    def eligible(item: ApplicationRequirementAssessment) -> bool:
        if item.interview_exhausted:
            return False
        if item.evidence_status in {
            EvidenceAssessmentStatus.SUFFICIENT, EvidenceAssessmentStatus.CONFIRMED_GAP,
            EvidenceAssessmentStatus.EVIDENCE_CONFIRMED, EvidenceAssessmentStatus.SKIPPED,
            EvidenceAssessmentStatus.NOT_APPLICABLE, EvidenceAssessmentStatus.EVIDENCE_CANDIDATE,
        }:
            return False
        if item.match_status == "matched":
            return False
        return not _EXCLUDED_TARGETS.search(item.original_requirement_text)

    def select(self, items: list[ApplicationRequirementAssessment]) -> ApplicationRequirementAssessment | None:
        seen: set[str] = set()
        eligible = []
        for item in items:
            canonical = normalize_text(item.canonical_requirement)
            if canonical in seen or not self.eligible(item):
                continue
            seen.add(canonical)
            group = (0 if item.requirement_level == "required" else 2)
            group += 0 if item.match_status == "partial" else 1
            related = 0 if item.linked_evidence_ids else 1
            impact = 0 if set(canonical.split()) & _HIGH_IMPACT else 1
            eligible.append(((group, related, impact, item.jd_order, item.assessment_id), item))
        return min(eligible, key=lambda pair: pair[0])[1] if eligible else None


def validate_candidate(answer: str, assessment: InterviewAnswerAssessment) -> tuple[str, str]:
    """V0.1 accepts only a literal supported answer span as a proposed claim."""
    from agent_runtime.evidence.extraction import career_fact_candidates, extract_evidence_candidates
    claim = (assessment.proposed_claim or "").strip()
    if not claim or not assessment.exact_supporting_quotes:
        raise InterviewValidationError("The answer needs a concrete supporting quote.")
    normalized_answer = normalize_text(answer)
    for quote in assessment.exact_supporting_quotes:
        if not normalize_text(quote) or normalize_text(quote) not in normalized_answer:
            raise InterviewValidationError("A supporting quote was not present in the answer.")
    if normalize_text(claim) not in normalized_answer:
        raise InterviewValidationError("The proposed claim must use the user's own wording.")
    if not any(normalize_text(claim) == normalize_text(q) for q in assessment.exact_supporting_quotes):
        raise InterviewValidationError("The proposed claim must equal an exact supporting quote.")
    extracted = career_fact_candidates(extract_evidence_candidates(claim))
    if extracted:
        selected = min(extracted, key=lambda item: (-item.confidence, len(item.source_quote)))
        if not _INSTRUCTION_IN_ANSWER.search(selected.source_quote):
            # Preserve literal user wording while excluding unrelated conversational text.
            return selected.source_quote, selected.source_quote
    if _INSTRUCTION_IN_ANSWER.search(answer):
        raise InterviewValidationError("The answer contains an instruction rather than verifiable experience.")
    return claim, claim
