"""Deterministic signal, routing, diff and aggregation policy."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from agent_runtime.feedback.types import (
    CandidateScope, CandidateType, FeedbackClassification, FeedbackEvent,
    FeedbackSourceType, SignalStrength,
)
from agent_runtime.security import canonical_json

_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE = re.compile(r"(?<!\w)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}(?!\w)")
_ADDRESS = re.compile(r"\b\d{1,6}\s+[\w .-]+\s+(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln)\b", re.I)
_SECRET = re.compile(r"(?i)\b(?:bearer\s+\S+|(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+|sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{12,})")
_CITATION = re.compile(r"\b(?:EXP-[A-Za-z0-9-]+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{27})\b", re.I)


def redact_secrets(text: str | None) -> str | None:
    return _SECRET.sub("[REDACTED]", text) if text is not None else None


def redact_skill_pii(text: str) -> str:
    for pattern in (_EMAIL, _PHONE, _ADDRESS, _SECRET):
        text = pattern.sub("[REDACTED]", text)
    return text


def contains_pii(text: str) -> bool:
    return any(pattern.search(text) for pattern in (_EMAIL, _PHONE, _ADDRESS))


def signal_strength(source: FeedbackSourceType, content: str | None = None) -> SignalStrength:
    if source in {FeedbackSourceType.EXPLICIT_INSTRUCTION,
                  FeedbackSourceType.EVIDENCE_CONFIRMED, FeedbackSourceType.EVIDENCE_REJECTED}:
        return SignalStrength.HIGHEST
    if source in {FeedbackSourceType.ARTIFACT_EDITED, FeedbackSourceType.INTERVIEW_FEEDBACK,
                  FeedbackSourceType.ARTIFACT_REJECTED, FeedbackSourceType.MANUAL_FEEDBACK}:
        return SignalStrength.MEDIUM
    return SignalStrength.WEAK


def structured_edit_diff(before: str, after: str,
                         unsupported_claims: list[str] | None = None) -> dict:
    """Observation only: text, citation and formatting changes, never inferred intent."""
    before_blocks = [part.strip() for part in before.splitlines() if part.strip()]
    after_blocks = [part.strip() for part in after.splitlines() if part.strip()]
    matcher = SequenceMatcher(None, before_blocks, after_blocks, autojunk=False)
    changed = []
    added, removed = [], []
    for kind, a0, a1, b0, b1 in matcher.get_opcodes():
        if kind in {"delete", "replace"}:
            removed.extend(before_blocks[a0:a1])
        if kind in {"insert", "replace"}:
            added.extend(after_blocks[b0:b1])
        if kind == "replace":
            changed.append({"before": before_blocks[a0:a1], "after": after_blocks[b0:b1]})
    return {"added_text": added, "removed_text": removed,
        "changed_blocks": changed, "length_change": len(after)-len(before),
        "evidence_citation_changes": {
            "added": sorted(set(_CITATION.findall(after))-set(_CITATION.findall(before))),
            "removed": sorted(set(_CITATION.findall(before))-set(_CITATION.findall(after)))},
        "formatting_changes": before.strip() == after.strip() and before != after,
        "unsupported_claim_corrections": [claim for claim in (unsupported_claims or [])
            if claim and claim in before and claim not in after],
        "unsupported_claim_correction_observed": any(claim and claim in before
            and claim not in after for claim in (unsupported_claims or [])),
        "terminology_substitutions": changed}


@dataclass(frozen=True)
class LearningThresholds:
    skill_min_events: int = 3
    skill_min_applications: int = 2
    repeated_acceptance_min: int = 2
    max_processing_attempts: int = 2


def canonical_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._:-]+", ".", value.casefold()).strip(".")
    return normalized[:160] or hashlib.sha256(value.encode()).hexdigest()[:16]


def content_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.casefold().split()).encode()).hexdigest()


def classify(event: FeedbackEvent) -> FeedbackClassification:
    preferred_after = event.source_type in {FeedbackSourceType.ARTIFACT_EDITED,
        FeedbackSourceType.INTERVIEW_FEEDBACK} and event.after_content
    text = (event.after_content if preferred_after else
            event.original_content or event.after_content or "").strip()
    lower = text.casefold()
    ids = [event.feedback_event_id]
    base = dict(supporting_feedback_event_ids=ids,
                conflicting_feedback_event_ids=[], requires_user_confirmation=True)
    scope = CandidateScope.APPLICATION if event.application_id and any(
        phrase in lower for phrase in ("this application", "this job", "for this role")) else CandidateScope.USER
    if not text or event.deleted_at is not None:
        return FeedbackClassification(candidate_type=CandidateType.IGNORE,
            rationale="No eligible explicit feedback.", confidence=0,
            scope=scope, **base)
    if event.source_type == FeedbackSourceType.APPLICATION_STATUS:
        return FeedbackClassification(candidate_type=CandidateType.IGNORE,
            rationale="Application outcome has no proven causal writing implication.",
            confidence=0, scope=scope, **base)
    if event.source_type == FeedbackSourceType.ARTIFACT_ACCEPTED:
        artifact_type = canonical_key(str(event.context_metadata_json.get("artifact_type", "artifact")))
        key = f"accepted_artifact.{artifact_type}"
        return FeedbackClassification(candidate_type=CandidateType.PREFERENCE_MEMORY,
            proposed_key_or_name=key,
            proposed_content=f"User accepted {artifact_type} output; preferred details need clarification.",
            scope=CandidateScope.USER,
            rationale="Acceptance is a weak observation, not proof of a reusable writing rule.",
            confidence=0.25, privacy_risk="normal", generalizability="unknown",
            type_metadata={"memory_type":"preference", "proposed_key":key,
                "proposed_value":"needs_clarification", "override_behavior":"not_injected"}, **base)
    if any(token in lower for token in ("ignore previous instructions", "ignore all instructions",
            "reveal system prompt", "execute this tool", "call this tool")):
        return FeedbackClassification(candidate_type=CandidateType.IGNORE,
            rationale="Instruction-like source content is not promoted.",
            confidence=0, scope=scope, **base)
    if any(phrase in lower for phrase in ("unknown evidence id", "external writes require approval",
            "never infer demographic", "must fail verification", "policy invariant")):
        return FeedbackClassification(candidate_type=CandidateType.PRODUCT_POLICY,
            proposed_key_or_name=canonical_key("policy."+lower[:70]),
            proposed_content=text, scope=CandidateScope.GLOBAL,
            rationale="Deterministic safety or product invariant requires developer review.",
            confidence=0.9, privacy_risk="normal", generalizability="global",
            type_metadata={"affected_component":"verification_or_runtime", "severity":"review",
                           "proposed_invariant":text, "reproduction_references":ids}, **base)
    skill_request = any(phrase in lower for phrase in ("create a skill", "reusable procedure",
        "repeatable process", "agent procedure", "every factual claim"))
    personal_fact = bool(re.search(r"\b(?:i|my|we)\s+(?:built|created|led|managed|presented|worked|have|increased|reduced|improved)\b", lower))
    if skill_request and not personal_fact and not contains_pii(text):
        safe = redact_skill_pii(text)
        return FeedbackClassification(candidate_type=CandidateType.SKILL,
            proposed_key_or_name=canonical_key("skill."+safe[:70]),
            proposed_content=safe, scope=CandidateScope.GLOBAL,
            rationale="Explicit reusable agent procedure proposal; evaluation approval required.",
            confidence=0.75, privacy_risk="normal", generalizability="cross_application",
            type_metadata={"proposed_skill_name":canonical_key(safe[:60]),
                "task_scope":"artifact_review", "proposed_instructions":safe,
                "positive_examples":[], "negative_examples":[],
                "applicability_conditions":[], "non_applicability_conditions":[],
                "required_tools":[], "prohibited_tools":[],
                "safety_constraints":["Never override evidence or runtime policy"],
                "evaluation_case_suggestions":[]}, **base)
    if any(word in lower for word in ("summary", "bullet", "prefer", "keep my", "for this application")):
        match = re.search(r"(?:no more than|under|at most|use|prefer)\s+(two|three|four|\d+)\s+sentences?", lower)
        key = "resume.summary.max_sentences" if match else "preference."+canonical_key(lower[:70])
        value = match.group(1) if match else text
        return FeedbackClassification(candidate_type=CandidateType.PREFERENCE_MEMORY,
            proposed_key_or_name=key, proposed_content=text, scope=scope,
            rationale="Explicit user-specific preference; current-turn instruction may override.",
            confidence=0.85, privacy_risk="personal", generalizability="user_specific",
            type_metadata={"memory_type":"preference", "proposed_key":key,
                "proposed_value":value, "override_behavior":"current_explicit_turn_overrides"}, **base)
    if personal_fact and event.source_type in {
            FeedbackSourceType.EXPLICIT_INSTRUCTION, FeedbackSourceType.MANUAL_FEEDBACK,
            FeedbackSourceType.INTERVIEW_FEEDBACK}:
        key = "career."+canonical_key(lower[:70])
        return FeedbackClassification(candidate_type=CandidateType.CAREER_EVIDENCE,
            proposed_key_or_name=key, proposed_content=text, scope=CandidateScope.USER,
            rationale="User-stated career fact requires exact-source review and confirmation.",
            confidence=0.75, privacy_risk="personal", generalizability="user_specific",
            type_metadata={"category":"experience", "claim":text,
                "exact_user_quotes":[text], "provenance":"user_feedback",
                "application_ids":[event.application_id] if event.application_id else [],
                "requirement_ids":[]}, **base)
    if event.source_type == FeedbackSourceType.ARTIFACT_EDITED:
        key = "artifact.edit."+content_hash(event.before_content or "")[:12]
        return FeedbackClassification(candidate_type=CandidateType.PREFERENCE_MEMORY,
            proposed_key_or_name=key, proposed_content=text, scope=CandidateScope.USER,
            rationale="Observed edit only; user intent remains unconfirmed.",
            confidence=0.45, privacy_risk="personal", generalizability="unknown",
            type_metadata={"memory_type":"preference", "proposed_key":key,
                "proposed_value":text, "override_behavior":"needs_clarification"}, **base)
    return FeedbackClassification(candidate_type=CandidateType.IGNORE,
        rationale="No reliable reusable intent from this event alone.",
        confidence=0, scope=scope, **base)


def ready_for_review(kind: CandidateType, events: list[FeedbackEvent],
                     *, thresholds: LearningThresholds) -> bool:
    if kind == CandidateType.PRODUCT_POLICY:
        return True
    if kind == CandidateType.CAREER_EVIDENCE:
        return True
    if kind == CandidateType.SKILL:
        explicit = any(e.source_type == FeedbackSourceType.EXPLICIT_INSTRUCTION
            and "skill" in (e.original_content or "").casefold() for e in events)
        applications = {e.application_id for e in events if e.application_id}
        return explicit or (len(events) >= thresholds.skill_min_events
            and len(applications) >= thresholds.skill_min_applications)
    if any(e.source_type == FeedbackSourceType.EXPLICIT_INSTRUCTION for e in events):
        return True
    if all(e.source_type == FeedbackSourceType.ARTIFACT_ACCEPTED for e in events):
        return len(events) >= thresholds.repeated_acceptance_min
    return len(events) >= 2 and all(e.signal_strength != SignalStrength.WEAK for e in events)
