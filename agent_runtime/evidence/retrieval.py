"""Deterministic, read-only selection of confirmed career evidence."""
from __future__ import annotations

import re
from dataclasses import dataclass

from agent_runtime.context.budget import estimate_tokens
from agent_runtime.evidence.repository import CareerEvidenceRepository
from agent_runtime.evidence.types import CareerEvidence, EvidenceStatus


@dataclass(frozen=True)
class SelectedEvidence:
    item: CareerEvidence
    rendered: str
    tokens: int


class CareerEvidenceRetriever:
    def __init__(self, repository: CareerEvidenceRepository) -> None:
        self._repository = repository

    def retrieve(self, *, query: str, application_id: str | None = None,
                 token_budget: int = 900, limit: int = 8) -> list[SelectedEvidence]:
        if token_budget <= 0 or limit <= 0:
            return []
        words = set(re.findall(r"[a-z0-9+#./-]{3,}", query.lower()))
        if application_id:
            linked = {link.evidence_id for link in self._repository.list_for_application(application_id)}
            candidates = [item for item in self._repository.list(status=EvidenceStatus.CONFIRMED, limit=500)
                          if item.evidence_id in linked]
        else:
            if not words:
                return []
            candidates = self._repository.list(status=EvidenceStatus.CONFIRMED, limit=500)
        ranked = []
        for item in candidates:
            tokens = set(re.findall(r"[a-z0-9+#./-]{3,}", item.current.claim_text.lower()))
            score = len(words & tokens)
            if application_id or score:
                ranked.append((-score, item.evidence_id, item))
        ranked.sort()
        selected: list[SelectedEvidence] = []
        remaining = token_budget
        for _, _, item in ranked:
            version = item.current
            text = (f"Evidence {item.evidence_id} version {version.version_number}\n"
                    f"Claim: {version.claim_text}\nProvenance: {version.source_type.value}")
            if version.exact_quote:
                text += f"\nExact quote: {version.exact_quote}"
            cost = estimate_tokens(text)
            if cost <= remaining:
                selected.append(SelectedEvidence(item, text, cost))
                remaining -= cost
            if len(selected) >= limit:
                break
        return selected
