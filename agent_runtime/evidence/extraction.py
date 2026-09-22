"""Deterministic candidate extraction before writing to the Career Evidence Vault."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from agent_runtime.evidence.types import CareerEvidence, EvidenceCandidate
from job_agent.domain import normalize_text

_PREFERENCE = re.compile(r"\b(?:prefer|preference|keep my|please make|for this one|writing style|tone)\b", re.I)
_INSTRUCTION = re.compile(r"^(?:please|can you|could you|write|add|remove|ignore|make|tell me)\b", re.I)
_FACT = re.compile(
    r"\b(?:i|we)\s+(?:built|created|designed|developed|implemented|improved|reduced|"
    r"increased|delivered|analyzed|managed|led|used|worked|studied|graduated|presented|"
    r"maintained|processed|have|am|was)\b|\b\d+(?:\.\d+)?%?\b",
    re.I,
)
_SPLIT_AND = re.compile(r"\s+and\s+(?=(?:i|we)\s+(?:built|created|designed|developed|implemented|improved|reduced|increased|delivered|analyzed|managed|led|used|presented|maintained|processed)\b)", re.I)


def _candidate_id(message_id: str | None, quote: str) -> str:
    digest = hashlib.sha256(f"{message_id or ''}|{normalize_text(quote)}".encode()).hexdigest()[:16]
    return f"EVC-{digest}"


def _fact_type(text: str) -> str:
    lower = text.casefold()
    if _PREFERENCE.search(text): return "preference"
    if re.search(r"\b\d+(?:\.\d+)?%?\b", text): return "metric"
    if re.search(r"\b(?:degree|university|college|graduat|studied)\b", lower): return "education"
    if re.search(r"\b(?:project|prototype)\b", lower): return "project"
    if re.search(r"\b(?:led|managed|responsible|maintained)\b", lower): return "responsibility"
    if re.search(r"\b(?:python|sql|java|tableau|docker|fastapi|pytorch|spark)\b", lower): return "skill"
    if re.search(r"\b(?:built|created|improved|reduced|increased|delivered|presented)\b", lower): return "achievement"
    return "other"


def _normalized_fact(quote: str) -> str:
    text = re.sub(r"^(?:I|We)\s+", "", quote.strip(), flags=re.I)
    text = text.rstrip(" .")
    return (text[:1].upper() + text[1:] + ".") if text else quote.strip()


def extract_evidence_candidates(
    message: str, *, message_id: str | None = None, session_id: str | None = None,
    source_context: str | None = None, existing: Sequence[CareerEvidence] = (),
    now: datetime | None = None,
) -> list[EvidenceCandidate]:
    """Extract literal, reviewable spans; conversational instructions never become facts."""
    extracted_at = now or datetime.now(UTC)
    spans: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|[\r\n]+", message.strip()):
        spans.extend(part.strip(" \t-•") for part in _SPLIT_AND.split(sentence) if part.strip())
    results: list[EvidenceCandidate] = []
    seen: set[str] = set()
    for quote in spans:
        kind = _fact_type(quote)
        is_preference = kind == "preference"
        if not is_preference and (_INSTRUCTION.search(quote) or not _FACT.search(quote)):
            continue
        normalized = _normalized_fact(quote)
        key = normalize_text(normalized)
        if key in seen:
            continue
        seen.add(key)
        conflicts = [item.evidence_id for item in existing
                     if normalize_text(item.current.claim_text) != key
                     and set(normalize_text(item.current.claim_text).split()) & set(key.split())
                     and item.current.employer_or_project]
        results.append(EvidenceCandidate(
            candidate_id=_candidate_id(message_id, quote), fact_type=kind,
            normalized_fact=normalized, source_quote=quote,
            source_message_id=message_id, source_context=source_context,
            confidence=.99 if is_preference or _FACT.search(quote) else .6,
            requires_confirmation=True, related_entity=None,
            session_id=session_id, extracted_at=extracted_at,
            conflict_evidence_ids=conflicts,
        ))
    return results


def validate_candidate_source(candidate: EvidenceCandidate, original_message: str) -> None:
    if candidate.source_quote not in original_message:
        raise ValueError("Evidence candidate source_quote must occur verbatim in the source message.")


def career_fact_candidates(candidates: Iterable[EvidenceCandidate]) -> list[EvidenceCandidate]:
    """Preferences remain outside the Vault's employment-evidence path."""
    return [item for item in candidates if item.fact_type != "preference"]
