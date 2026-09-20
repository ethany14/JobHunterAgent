"""Public career-evidence contracts."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvidenceStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class EvidenceSourceType(StrEnum):
    RESUME = "resume"
    USER_ATTESTED = "user_attested"
    INTERVIEW = "interview"
    DOCUMENT = "document"
    PROJECT = "project"
    IMPORTED_PROFILE = "imported_profile"


class EvidenceCategory(StrEnum):
    EXPERIENCE = "experience"
    PROJECT = "project"
    SKILL = "skill"
    EDUCATION = "education"
    ACHIEVEMENT = "achievement"
    LEADERSHIP = "leadership"
    RESPONSIBILITY = "responsibility"


class EvidenceLinkType(StrEnum):
    SUPPORTS = "supports"
    PARTIALLY_SUPPORTS = "partially_supports"
    RELATED = "related"


class EvidenceEventType(StrEnum):
    EVIDENCE_CREATED = "EVIDENCE_CREATED"
    SOURCE_VALIDATED = "SOURCE_VALIDATED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    REVISION_PROPOSED = "REVISION_PROPOSED"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"
    RESTORED = "RESTORED"
    APPLICATION_LINKED = "APPLICATION_LINKED"
    APPLICATION_UNLINKED = "APPLICATION_UNLINKED"


class EvidenceMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1)
    unit: str | None = None
    qualifier: str | None = None
    original_text: str = Field(min_length=1)


class EvidenceVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_version_id: str
    evidence_id: str
    version_number: int
    category: EvidenceCategory
    claim_text: str
    source_type: EvidenceSourceType
    source_reference: str | None = None
    exact_quote: str | None = None
    source_section: str | None = None
    employer_or_project: str | None = None
    role: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    technologies: list[str] = Field(default_factory=list)
    metrics: list[EvidenceMetric] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_by: str
    created_at: datetime
    content_hash: str
    external_evidence_id: str | None = None
    source_run_id: str | None = None


class CareerEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    status: EvidenceStatus
    current_version: int
    version: int
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None = None
    rejected_at: datetime | None = None
    archived_at: datetime | None = None
    current: EvidenceVersion


class CareerEvidenceEvent(BaseModel):
    event_id: str
    evidence_id: str
    sequence: int
    event_type: str
    evidence_version_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class EvidenceApplicationLink(BaseModel):
    link_id: str
    evidence_id: str
    evidence_version_id: str
    application_id: str
    requirement_id: str | None = None
    canonical_requirement: str | None = None
    link_type: EvidenceLinkType
    created_at: datetime
    created_by: str
