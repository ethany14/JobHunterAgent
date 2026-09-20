"""Safe public request schemas for the local Career Evidence Vault."""
from typing import Any

from pydantic import BaseModel, Field

from agent_runtime.evidence.types import EvidenceCategory, EvidenceLinkType, EvidenceMetric, EvidenceSourceType


class CreateEvidenceCandidate(BaseModel):
    category: EvidenceCategory
    claim_text: str = Field(min_length=1, max_length=5000)
    source_type: EvidenceSourceType
    source_reference: str | None = Field(default=None, max_length=256)
    exact_quote: str | None = Field(default=None, max_length=5000)
    source_section: str | None = Field(default=None, max_length=256)
    employer_or_project: str | None = Field(default=None, max_length=256)
    role: str | None = Field(default=None, max_length=256)
    start_date: str | None = None
    end_date: str | None = None
    technologies: list[str] = Field(default_factory=list)
    metrics: list[EvidenceMetric] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class EvidenceMutation(BaseModel):
    expected_version: int = Field(ge=1)


class RejectEvidence(EvidenceMutation):
    reason: str = Field(default="", max_length=1000)


class ProposeEvidenceRevision(EvidenceMutation):
    changes: dict[str, Any]


class LinkEvidence(BaseModel):
    evidence_id: str
    expected_version: int = Field(ge=1)
    link_type: EvidenceLinkType = EvidenceLinkType.SUPPORTS
    requirement_id: str | None = None
    canonical_requirement: str | None = None
