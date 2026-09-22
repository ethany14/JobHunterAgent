"""Deterministic resume content-quality validation and cleanup."""
from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from job_agent.schemas import (
    ResumeQualityIssue, ResumeQualityResult, ResumeSection, ResumeSourceEntry,
    SupportedClaim, TailoredResume,
)

_BULLET_PREFIX = re.compile(r"^\s*(?:[-*•◦+|]|\d+[.)])\s*")
_TOKENS = re.compile(r"[a-z0-9+#./]+")
_ACTION_WORDS = frozenset({
    "built", "created", "designed", "developed", "implemented", "improved",
    "increased", "reduced", "delivered", "analyzed", "led", "managed",
})


class DuplicateClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kept_claim_id: str
    removed_claim_id: str
    similarity: float = Field(ge=0, le=1)
    reason: str


def normalize_claim_text(text: str) -> str:
    cleaned = _BULLET_PREFIX.sub("", text).casefold()
    cleaned = re.sub(r"[^\w+#./\s]", " ", cleaned)
    # Keep meaningful internal punctuation (Node.js, CI/CD) but ignore prose punctuation.
    cleaned = re.sub(r"(?<!\w)[./]|[./](?!\w)", " ", cleaned)
    return " ".join(cleaned.split())


def claim_token_set(text: str) -> set[str]:
    return set(_TOKENS.findall(normalize_claim_text(text)))


def claim_similarity(left: str, right: str) -> float:
    left_normalized = normalize_claim_text(left)
    right_normalized = normalize_claim_text(right)
    if left_normalized == right_normalized:
        return 1.0
    left_tokens, right_tokens = claim_token_set(left), claim_token_set(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _strength(claim: SupportedClaim, index: int) -> tuple[int, int, int, int]:
    tokens = claim_token_set(claim.text)
    specificity = len(claim.evidence_ids)
    action_outcome = int(bool(tokens & _ACTION_WORDS)) + int(bool(re.search(r"\d", claim.text)))
    coverage = len(claim.target_requirement_ids)
    return specificity, action_outcome, coverage, -index


def find_duplicate_claims(
    claims: Sequence[SupportedClaim], threshold: float = 0.82,
) -> list[DuplicateClaim]:
    duplicates: list[DuplicateClaim] = []
    removed: set[str] = set()
    for left_index, left in enumerate(claims):
        if left.claim_id in removed:
            continue
        for right_index in range(left_index + 1, len(claims)):
            right = claims[right_index]
            if right.claim_id in removed:
                continue
            similarity = claim_similarity(left.text, right.text)
            if similarity < threshold:
                continue
            keep, discard = (left, right)
            if _strength(right, right_index) > _strength(left, left_index):
                keep, discard = right, left
            duplicates.append(DuplicateClaim(
                kept_claim_id=keep.claim_id,
                removed_claim_id=discard.claim_id,
                similarity=round(similarity, 4),
                reason="normalized_exact_match" if similarity == 1 else "lexical_similarity",
            ))
            removed.add(discard.claim_id)
            if discard is left:
                break
    return duplicates


def deduplicate_claims(
    claims: Sequence[SupportedClaim], threshold: float = 0.82,
) -> tuple[list[SupportedClaim], list[DuplicateClaim]]:
    duplicates = find_duplicate_claims(claims, threshold)
    removed = {item.removed_claim_id for item in duplicates}
    return [claim for claim in claims if claim.claim_id not in removed], duplicates


def clean_tailored_resume(
    resume: TailoredResume, *, threshold: float = 0.82,
) -> tuple[TailoredResume, list[DuplicateClaim]]:
    """Remove predictable duplicates while preserving the first section/entry layout."""
    all_claims = resume.claims()
    _, duplicates = deduplicate_claims(all_claims, threshold)
    removed = {item.removed_claim_id for item in duplicates}
    sections: list[ResumeSection] = []
    for section in resume.sections:
        entries = []
        skill_names: dict[str, str] = {}
        for entry in section.entries:
            bullets = []
            for claim in entry.bullets:
                if claim.claim_id in removed:
                    continue
                if section.section_type == "skills":
                    canonical = normalize_claim_text(claim.text).replace("postgresql", "postgres")
                    if canonical in skill_names:
                        duplicates.append(DuplicateClaim(
                            kept_claim_id=skill_names[canonical],
                            removed_claim_id=claim.claim_id,
                            similarity=1.0,
                            reason="canonical_skill_alias",
                        ))
                        continue
                    skill_names[canonical] = claim.claim_id
                bullets.append(claim)
            entries.append(entry.model_copy(update={"bullets": bullets}))
        sections.append(section.model_copy(update={"entries": entries}))
    adjustments = list(resume.quality_adjustments)
    adjustments.extend(
        f"Removed duplicate claim {item.removed_claim_id}; retained {item.kept_claim_id}."
        for item in duplicates
    )
    cleaned = TailoredResume.model_validate({
        **resume.model_dump(mode="python"),
        "sections": [item.model_dump(mode="python") for item in sections],
        "quality_adjustments": adjustments,
    })
    return cleaned, duplicates


def validate_resume_structure(
    resume: TailoredResume,
    source_entries: Sequence[ResumeSourceEntry],
    *,
    known_evidence_ids: set[str] | None = None,
) -> list[ResumeQualityIssue]:
    issues: list[ResumeQualityIssue] = []
    sources = {entry.source_entry_id: entry for entry in source_entries}
    claims_by_section: dict[str, list[SupportedClaim]] = {}
    for section in resume.sections:
        claims = [claim for entry in section.entries for claim in entry.bullets]
        claims_by_section.setdefault(section.section_type, []).extend(claims)
        if not section.entries or not claims:
            issues.append(ResumeQualityIssue(
                code="empty_section", message=f"{section.title} contains no claims.",
                section_type=section.section_type,
            ))
        for entry in section.entries:
            for claim in entry.bullets:
                if not claim.evidence_ids:
                    issues.append(ResumeQualityIssue(
                        code="claim_without_evidence", message="Claim has no resume evidence.",
                        claim_ids=[claim.claim_id], section_type=section.section_type,
                    ))
                unknown = [item for item in claim.evidence_ids
                           if known_evidence_ids is not None and item not in known_evidence_ids]
                if unknown:
                    issues.append(ResumeQualityIssue(
                        code="unknown_evidence_id",
                        message=f"Claim cites unknown evidence IDs: {', '.join(unknown)}.",
                        claim_ids=[claim.claim_id], section_type=section.section_type,
                    ))
                source = sources.get(claim.source_entry_id)
                if source is None and not claim.source_entry_id.startswith("legacy:"):
                    issues.append(ResumeQualityIssue(
                        code="unknown_source_entry", message="Claim cites an unknown source entry.",
                        claim_ids=[claim.claim_id], section_type=section.section_type,
                    ))
                if source and not set(claim.evidence_ids).issubset(source.evidence_ids):
                    issues.append(ResumeQualityIssue(
                        code="invalid_section_membership",
                        message="Claim cites evidence owned by another source entry.",
                        claim_ids=[claim.claim_id], section_type=section.section_type,
                    ))
                allowed = {
                    "summary": {"summary", "experience", "project", "skills", "other"},
                    "experience": {"experience"}, "projects": {"project"},
                    "education": {"education"}, "skills": {"skills", "experience", "project", "other"},
                }[section.section_type]
                if source and source.entry_type not in allowed:
                    issues.append(ResumeQualityIssue(
                        code="invalid_section_membership",
                        message=f"{source.entry_type} source cannot appear in {section.section_type}.",
                        claim_ids=[claim.claim_id], section_type=section.section_type,
                    ))
                if section.section_type == "skills" and re.search(r"[,;|•]|\band\b", claim.text, re.I):
                    issues.append(ResumeQualityIssue(
                        code="unformatted_skill_block",
                        message="Skills must be separate normalized items.",
                        claim_ids=[claim.claim_id], section_type="skills",
                    ))

    summary = claims_by_section.get("summary", [])
    summary_text = " ".join(claim.text for claim in summary)
    sentence_count = len([item for item in re.split(r"(?<=[.!?])\s+", summary_text.strip()) if item])
    if sentence_count > 2:
        issues.append(ResumeQualityIssue(
            code="summary_too_long", message="Professional summary must be at most two sentences.",
            claim_ids=[item.claim_id for item in summary], section_type="summary",
        ))
    non_summary = [claim for kind, claims in claims_by_section.items() if kind != "summary" for claim in claims]
    for summary_claim in summary:
        for other in non_summary:
            if claim_similarity(summary_claim.text, other.text) >= .82:
                issues.append(ResumeQualityIssue(
                    code="summary_repeats_bullet",
                    message="Professional summary substantially repeats another resume bullet.",
                    claim_ids=[summary_claim.claim_id, other.claim_id], section_type="summary",
                ))
    for duplicate in find_duplicate_claims(resume.claims()):
        issues.append(ResumeQualityIssue(
            code="duplicate_claim", message="Resume contains substantially duplicate claims.",
            claim_ids=[duplicate.kept_claim_id, duplicate.removed_claim_id],
        ))
    skills = claims_by_section.get("skills", [])
    seen: dict[str, str] = {}
    for claim in skills:
        name = normalize_claim_text(claim.text).replace("postgresql", "postgres")
        if name in seen:
            issues.append(ResumeQualityIssue(
                code="duplicate_skill", message="Skill appears more than once.",
                claim_ids=[seen[name], claim.claim_id], section_type="skills",
            ))
        seen[name] = claim.claim_id
    return issues


def quality_result(issues: Sequence[ResumeQualityIssue]) -> ResumeQualityResult:
    unique_feedback = list(dict.fromkeys(item.message for item in issues))
    return ResumeQualityResult(
        passed=not issues, issues=list(issues), revision_feedback=unique_feedback,
    )

