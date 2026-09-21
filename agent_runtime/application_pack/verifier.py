"""Conservative verifier shared by resume, cover letter and answer artifacts."""
from __future__ import annotations

import re

from agent_runtime.application_pack.types import (
    ApplicationAnswer, ArtifactVerification, ClaimIssue, CoverLetter,
    EvidenceSnapshotItem, GroundedBlock,
)
from job_agent.domain import normalize_text
from job_agent.schemas import TailoredResume


def _blocks(content: dict, artifact_type: str) -> list[GroundedBlock]:
    if artifact_type == "cover_letter":
        return CoverLetter.model_validate(content).blocks
    if artifact_type == "application_answer":
        return ApplicationAnswer.model_validate(content).answer_blocks
    resume = TailoredResume.model_validate(content)
    return [GroundedBlock(block_id=f"resume-{index}", text=claim.text,
                          block_type="factual", evidence_ids=claim.evidence_ids,
                          evidence_version_ids=claim.evidence_ids)
            for index, claim in enumerate(
                resume.professional_summary + resume.experience_bullets + resume.highlighted_skills)]


def _directly_supported(claim: str, evidence: EvidenceSnapshotItem) -> bool:
    target = normalize_text(claim)
    for source in (evidence.claim_text, evidence.exact_quote or ""):
        normalized = normalize_text(source)
        start = normalized.find(target)
        if start < 0:
            continue
        preceding = re.split(r"\b(?:but|however|while)\b|[.;]", normalized[:start])[-1]
        following = re.split(r"\b(?:but|however|while)\b|[.;]", normalized[start + len(target):])[0]
        nearby = " ".join(preceding.split()[-3:] + following.split()[:3])
        if not re.search(r"\b(?:no|not|never|without|lack(?:s|ed|ing)?|didn't|haven't)\b", nearby):
            return True
    return False


class ArtifactVerifier:
    """Only literal supported claims pass in v0.1; no semantic guesswork."""

    def verify(self, content: dict, artifact_type: str,
               evidence: list[EvidenceSnapshotItem], *, max_length: int | None = None,
               expected_question: str | None = None,
               preferences: list[dict] | None = None) -> ArtifactVerification:
        by_id = {item.evidence_id: item for item in evidence}
        by_version = {item.evidence_version_id: item for item in evidence}
        issues: list[ClaimIssue] = []
        blocks = _blocks(content, artifact_type)
        if artifact_type == "tailored_resume":
            for preference in preferences or []:
                if preference.get("memory_key") != "resume.summary.max_sentences":
                    continue
                configured = (preference.get("content") or {}).get("max_sentences")
                if configured is None:
                    match = re.search(r"no more than (\d+|one|two|three|four) sentences?",
                                      preference.get("display_text", ""), re.I)
                    if match:
                        configured = {"one": 1, "two": 2, "three": 3, "four": 4}.get(
                            match.group(1).lower(), match.group(1))
                if configured is None:
                    continue
                try: maximum = int(configured)
                except (TypeError, ValueError): continue
                if maximum < 1: continue
                resume = TailoredResume.model_validate(content)
                summary = " ".join(block.text for block in resume.professional_summary)
                count = len([part for part in re.split(r"(?<=[.!?])\s+", summary.strip()) if part])
                if count > maximum:
                    issues.append(ClaimIssue(block_id="professional_summary", unsupported_text=summary,
                        reason="Confirmed summary sentence preference was exceeded.", cited_evidence_ids=[],
                        revision_instruction=f"Use at most {maximum} summary sentences."))
        if artifact_type == "application_answer" and expected_question is not None:
            if ApplicationAnswer.model_validate(content).question != expected_question:
                issues.append(ClaimIssue(block_id="question", unsupported_text="",
                    reason="The answer changed the user's application question.",
                    cited_evidence_ids=[], revision_instruction="Answer the original question."))
        if artifact_type == "cover_letter":
            letter = CoverLetter.model_validate(content)
            if letter.greeting and letter.greeting.strip().casefold() not in {
                "dear hiring manager", "dear hiring manager,", "dear hiring team",
                "dear hiring team,", "hello", "hello,",
            }:
                issues.append(ClaimIssue(block_id="greeting", unsupported_text=letter.greeting,
                    reason="Greeting may invent a hiring-manager name.", cited_evidence_ids=[],
                    revision_instruction="Use a generic greeting or leave it blank."))
            if letter.closing and re.search(r"\b(i|my|we|our|years?|built|led|managed|experience)\b|\d",
                                            letter.closing, re.I):
                issues.append(ClaimIssue(block_id="closing", unsupported_text=letter.closing,
                    reason="Closing contains an unverified candidate claim.", cited_evidence_ids=[],
                    revision_instruction="Use a neutral closing."))
        full_text = " ".join(block.text for block in blocks)
        if artifact_type == "cover_letter":
            letter = CoverLetter.model_validate(content)
            full_text = " ".join(filter(None, [letter.greeting, full_text, letter.closing]))
        if max_length is not None and len(full_text) > max_length:
            issues.append(ClaimIssue(block_id="length", unsupported_text="",
                reason="Character limit exceeded.", cited_evidence_ids=[],
                revision_instruction=f"Shorten to at most {max_length} characters."))
        for block in blocks:
            citations = [by_id.get(identifier) or by_version.get(identifier)
                         for identifier in block.evidence_ids + block.evidence_version_ids]
            citations = [item for item in citations if item is not None]
            valid_ids = bool(block.evidence_ids or block.evidence_version_ids) and all(
                identifier in by_id or identifier in by_version
                for identifier in block.evidence_ids + block.evidence_version_ids)
            if valid_ids and block.evidence_ids and block.evidence_version_ids:
                resolved = [by_id.get(value) or by_version.get(value) for value in block.evidence_ids]
                valid_ids = {value.evidence_version_id for value in resolved} == set(block.evidence_version_ids)
            factual = block.block_type.value == "factual" or bool(re.search(
                r"\b(years?|built|led|managed|developed|used|deployed|have|has|bring|possess|"
                r"increased|reduced|experienced?|skilled|expert|proficient|proven|"
                r"delivered|achieved|contributed|background|familiar|certified)\b|\d",
                block.text, re.I))
            if (block.evidence_ids or block.evidence_version_ids) and not valid_ids:
                reason = "Citation does not resolve to a matching confirmed evidence version."
            elif factual and artifact_type != "tailored_resume" and not block.evidence_version_ids:
                reason = "Factual claim must cite a confirmed evidence version."
            elif factual and (not valid_ids or not citations):
                reason = "Factual claim lacks a confirmed evidence version in this generation snapshot."
            elif factual and not any(_directly_supported(block.text, item) for item in citations):
                reason = "The cited evidence does not directly support the complete claim."
            else:
                reason = None
            if reason:
                issues.append(ClaimIssue(block_id=block.block_id,
                    unsupported_text=block.text, reason=reason,
                    cited_evidence_ids=block.evidence_ids,
                    revision_instruction="Use an exact supported excerpt or remove the claim."))
        return ArtifactVerification(passed=not issues, issues=issues)
