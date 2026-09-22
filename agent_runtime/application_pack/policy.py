"""Deterministic, application-scoped evidence selection and question gating."""
from __future__ import annotations

import hashlib
import re

from agent_runtime.application_pack.types import EvidenceSnapshotItem
from agent_runtime.context.budget import estimate_tokens
from agent_runtime.evidence.types import (
    CareerEvidence, EvidenceApplicationLink, EvidenceSourceType, EvidenceStatus,
)
from agent_runtime.security import canonical_json
from job_agent.schemas import JobAnalysis


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#./-]{2,}", value.casefold()))


_INSTRUCTION_TEXT = re.compile(
    r"\b(ignore (?:previous|prior|all) instructions|system prompt|developer message|"
    r"override (?:the )?(?:verifier|safety|rules)|do not verify)\b", re.I)


class EvidenceSelectionPolicy:
    def select(self, *, evidence: list[CareerEvidence], links: list[EvidenceApplicationLink],
               job: JobAnalysis, token_budget: int = 1800) -> list[EvidenceSnapshotItem]:
        generic_words = {"with", "from", "have", "experience", "experienced", "working",
                         "work", "team", "teams", "role", "candidate", "strong", "skills",
                         "ability", "using", "good", "the", "and", "for", "this", "that"}
        context_words = _words(" ".join([job.title, job.summary, *job.responsibilities])) - generic_words
        by_id = {item.evidence_id: item for item in evidence
                 if item.status == EvidenceStatus.CONFIRMED
                 and not _INSTRUCTION_TEXT.search(item.current.claim_text)}
        linked: dict[str, list[EvidenceApplicationLink]] = {}
        for link in links:
            if link.evidence_id in by_id and link.evidence_version_id == by_id[link.evidence_id].current.evidence_version_id:
                linked.setdefault(link.evidence_id, []).append(link)
        ranked = []
        for item in by_id.values():
            # An Interviewer answer remains application-scoped until explicitly linked here.
            if item.current.source_type == EvidenceSourceType.INTERVIEW and item.evidence_id not in linked:
                continue
            claim_words = _words(item.current.claim_text)
            required = [req for req in job.requirements if req.level == "required"
                        and claim_words & _words(req.canonical_name + " " + req.atomic_text)]
            preferred = [req for req in job.requirements if req.level == "preferred"
                         and claim_words & _words(req.canonical_name + " " + req.atomic_text)]
            if item.evidence_id in linked:
                rank, reason = 0, "application_link"
            elif required:
                rank, reason = 1, "required_requirement"
            elif preferred:
                rank, reason = 2, "preferred_requirement"
            elif claim_words & context_words:
                rank, reason = 3, "other_relevant_confirmed"
            else:
                continue
            associated = sorted({link.requirement_id for link in linked.get(item.evidence_id, [])
                                 if link.requirement_id} | {req.requirement_id for req in required + preferred})
            ranked.append((rank, -len(required), item.evidence_id, item, reason, associated))
        ranked.sort()
        selected, remaining = [], token_budget
        for _, _, _, item, reason, associated in ranked:
            version = item.current
            cost = estimate_tokens({"claim": version.claim_text, "quote": version.exact_quote,
                                    "id": version.evidence_version_id})
            if cost > remaining:
                continue
            selected.append(EvidenceSnapshotItem(
                evidence_id=item.evidence_id, evidence_version_id=version.evidence_version_id,
                content_hash=version.content_hash, source_type=version.source_type.value,
                selection_reason=reason, associated_requirement_ids=associated,
                claim_text=version.claim_text, exact_quote=version.exact_quote,
                source_section=version.source_section,
                category=version.category.value,
                employer_or_project=version.employer_or_project,
                role=version.role,
                start_date=version.start_date,
                end_date=version.end_date,
            ))
            remaining -= cost
        return selected


def evidence_set_hash(items: list[EvidenceSnapshotItem], *, job_snapshot_id: str,
                      preferences: list[dict], prompt_version: str,
                      model_configuration: dict | None = None) -> str:
    payload = {"job_snapshot_id": job_snapshot_id, "prompt_version": prompt_version,
               "evidence": [item.model_dump(mode="json") for item in items],
               "preferences": preferences,
               "model_configuration": model_configuration or {}}
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


_DEMOGRAPHIC = re.compile(r"\b(disability|veteran status|race|ethnicity|gender|sexual orientation|religion|pregnan)\w*\b", re.I)
_LEGAL = re.compile(r"\b(sponsor(?:ship)?|work authori[sz]ation|citizen(?:ship)?|criminal history|background check)\b", re.I)
_LOGISTICS = re.compile(r"\b(salary|compensation|relocat|availab|start date|travel)\w*\b", re.I)


def classify_question(question: str) -> str:
    if _DEMOGRAPHIC.search(question): return "demographic"
    if _LEGAL.search(question): return "legal_or_identity"
    if _LOGISTICS.search(question): return "logistics"
    lowered = question.casefold()
    if re.search(r"\b(why (?:this|our) company|interested in (?:this|our) company)\b", lowered): return "company_interest"
    if re.search(r"\b(why|motivat|interested)\b", lowered): return "motivation"
    if re.search(r"\b(experience|used|built|worked with|project)\b", lowered): return "experience"
    if re.search(r"\b(fit|qualified|strength)\b", lowered): return "role_fit"
    return "unknown"


def requires_manual_answer(category: str) -> bool:
    return category in {"logistics", "legal_or_identity", "demographic", "unknown"}
