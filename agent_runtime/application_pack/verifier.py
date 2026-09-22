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


def _contains_supported_text(claim: str, evidence: EvidenceSnapshotItem) -> bool:
    target = normalize_text(claim)
    return any(normalize_text(source) in target for source in (
        evidence.claim_text, evidence.exact_quote or ""
    ) if normalize_text(source))


class ArtifactVerifier:
    """Only literal supported claims pass in v0.1; no semantic guesswork."""

    def verify(self, content: dict, artifact_type: str,
               evidence: list[EvidenceSnapshotItem], *, max_length: int | None = None,
               expected_question: str | None = None,
               preferences: list[dict] | None = None,
               expected_role: str | None = None,
               resume_sentences: list[str] | None = None) -> ArtifactVerification:
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
            answer = ApplicationAnswer.model_validate(content)
            if answer.question != expected_question:
                issues.append(ClaimIssue(block_id="question", unsupported_text="",
                    reason="The answer changed the user's application question.",
                    cited_evidence_ids=[], revision_instruction="Answer the original question."))
            for block in answer.answer_blocks:
                cited = [by_id.get(identifier) or by_version.get(identifier)
                         for identifier in block.evidence_ids + block.evidence_version_ids]
                cited = [item for item in cited if item is not None]
                normalized = normalize_text(block.text)
                if cited and any(normalized == normalize_text(source)
                    for item in cited for source in (item.claim_text, item.exact_quote or "")
                    if normalize_text(source)):
                    issues.append(ClaimIssue(block_id=block.block_id,
                        unsupported_text=block.text,
                        reason="Application answer merely copies a resume evidence sentence.",
                        cited_evidence_ids=block.evidence_ids,
                        revision_instruction=(
                            "Answer the question directly and integrate the supported evidence into natural prose.")))
        if artifact_type == "cover_letter":
            letter = CoverLetter.model_validate(content)
            if expected_role and len(letter.paragraphs) < 3:
                issues.append(ClaimIssue(block_id="structure", unsupported_text="",
                    reason="Cover letter needs at least three coherent paragraphs.",
                    cited_evidence_ids=[], revision_instruction=(
                        "Write an opening, one or two evidence-based fit paragraphs, and a concise closing paragraph.")))
            if expected_role and letter.paragraphs and letter.paragraphs[0].paragraph_type != "opening":
                issues.append(ClaimIssue(block_id="opening",
                    unsupported_text=letter.paragraphs[0].text,
                    reason="Cover letter does not begin with a role-specific opening.",
                    cited_evidence_ids=letter.paragraphs[0].evidence_ids,
                    revision_instruction="Begin with a concise opening for the verified target role."))
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
            seen_paragraphs: dict[str, str] = {}
            for index, paragraph in enumerate(letter.paragraphs, start=1):
                normalized = normalize_text(paragraph.text)
                if normalized in seen_paragraphs:
                    issues.append(ClaimIssue(block_id=f"paragraph-{index}",
                        unsupported_text=paragraph.text, reason="Cover letter repeats a paragraph.",
                        cited_evidence_ids=paragraph.evidence_ids,
                        revision_instruction="Remove the repeated paragraph."))
                seen_paragraphs[normalized] = paragraph.text
                cited = [by_id.get(identifier) or by_version.get(identifier)
                         for identifier in paragraph.evidence_ids + paragraph.evidence_version_ids]
                cited = [item for item in cited if item is not None]
                if cited and any(normalized == normalize_text(source)
                    for item in cited for source in (item.claim_text, item.exact_quote or "")
                    if normalize_text(source)):
                    issues.append(ClaimIssue(block_id=f"paragraph-{index}",
                        unsupported_text=paragraph.text,
                        reason="Cover letter paragraph merely copies a resume evidence sentence.",
                        cited_evidence_ids=paragraph.evidence_ids,
                        revision_instruction=(
                            "Integrate the evidence into natural prose and explain its relevance to the role.")))
                for sentence in resume_sentences or []:
                    if normalized and normalized == normalize_text(sentence):
                        issues.append(ClaimIssue(block_id=f"paragraph-{index}",
                            unsupported_text=paragraph.text,
                            reason="Cover letter repeats a resume sentence verbatim.",
                            cited_evidence_ids=paragraph.evidence_ids,
                            revision_instruction="Connect the evidence to the employer need without copying the resume sentence."))
            generic_phrases = (
                "i am writing to express my interest", "i believe i am the ideal candidate",
                "please find my resume attached",
            )
            generic_count = sum(normalize_text(phrase) in normalize_text(paragraph.text)
                                for phrase in generic_phrases for paragraph in letter.paragraphs)
            if generic_count > 0:
                issues.append(ClaimIssue(block_id="template-language", unsupported_text="",
                    reason="Cover letter contains generic template language.", cited_evidence_ids=[],
                    revision_instruction="Replace generic phrases with role-specific, evidence-backed fit."))
            if len(letter.paragraphs) >= 3 and expected_role and normalize_text(expected_role) not in normalize_text(
                " ".join(paragraph.text for paragraph in letter.paragraphs[:2])
            ):
                issues.append(ClaimIssue(block_id="opening", unsupported_text=letter.paragraphs[0].text,
                    reason="Opening does not reference the target role.", cited_evidence_ids=[],
                    revision_instruction="Reference the verified target role in the opening."))
        full_text = " ".join(block.text for block in blocks)
        if artifact_type == "cover_letter":
            letter = CoverLetter.model_validate(content)
            full_text = " ".join(filter(None, [letter.greeting, full_text, letter.closing]))
        effective_max_length = max_length if max_length is not None else (
            4_000 if artifact_type == "cover_letter" else None
        )
        if effective_max_length is not None and len(full_text) > effective_max_length:
            issues.append(ClaimIssue(block_id="length", unsupported_text="",
                reason="Character limit exceeded.", cited_evidence_ids=[],
                revision_instruction=f"Shorten to at most {effective_max_length} characters."))
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
            elif factual and not any(
                    _directly_supported(block.text, item)
                    or (artifact_type in {"cover_letter", "application_answer"}
                        and _contains_supported_text(block.text, item))
                    for item in citations):
                reason = "The cited evidence does not directly support the complete claim."
            else:
                reason = None
            if reason:
                issues.append(ClaimIssue(block_id=block.block_id,
                    unsupported_text=block.text, reason=reason,
                    cited_evidence_ids=block.evidence_ids,
                    revision_instruction=("Integrate exact supported evidence into natural prose without "
                        "adding facts, or remove the unsupported claim.")))
        return ArtifactVerification(passed=not issues, issues=issues)
