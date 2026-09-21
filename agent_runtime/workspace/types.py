"""Validated Job Workspace contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApplicationStatus(StrEnum):
    SAVED = "saved"
    ANALYZING = "analyzing"
    ANALYSIS_FAILED = "analysis_failed"
    NEEDS_EVIDENCE = "needs_evidence"
    MATERIALS_READY = "materials_ready"
    READY_TO_APPLY = "ready_to_apply"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    ARCHIVED = "archived"


class ArtifactType(StrEnum):
    JOB_ANALYSIS = "job_analysis"
    MATCH_REPORT = "match_report"
    TAILORED_RESUME = "tailored_resume"
    COVER_LETTER = "cover_letter"
    APPLICATION_ANSWER = "application_answer"
    RECRUITER_MESSAGE = "recruiter_message"
    INTERVIEW_BRIEF = "interview_brief"


class ArtifactStatus(StrEnum):
    DRAFT = "draft"
    VERIFIED = "verified"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class ApplicationEventType(StrEnum):
    APPLICATION_CREATED = "application_created"
    SNAPSHOT_ATTACHED = "snapshot_attached"
    STATUS_CHANGED = "status_changed"
    RUN_ATTACHED = "run_attached"
    SESSION_ATTACHED = "session_attached"
    ARTIFACT_CREATED = "artifact_created"
    ARTIFACT_VERIFIED = "artifact_verified"
    ARTIFACT_APPROVED = "artifact_approved"
    ARTIFACT_SUPERSEDED = "artifact_superseded"
    NEXT_ACTION_UPDATED = "next_action_updated"
    DEADLINE_UPDATED = "deadline_updated"


class JobRecord(WorkspaceModel):
    job_id: str
    canonical_url: str | None = None
    source_site: str | None = None
    company: str | None = None
    title: str | None = None
    location: str | None = None
    created_at: datetime
    updated_at: datetime


class JobSnapshotRecord(WorkspaceModel):
    snapshot_id: str
    job_id: str
    content_hash: str
    raw_page_text: str | None = None
    cleaned_job_description: str
    extraction_metadata: dict[str, Any] = Field(default_factory=dict)
    captured_at: datetime


class JobResolution(WorkspaceModel):
    job: JobRecord
    snapshot: JobSnapshotRecord
    created: bool
    snapshot_created: bool
    duplicate_candidates: list[str] = Field(default_factory=list)


class WorkspaceResolution(WorkspaceModel):
    job: JobRecord
    snapshot: JobSnapshotRecord
    application: "ApplicationRecord"
    created_job: bool
    created_snapshot: bool
    created_application: bool
    duplicate_detected: bool


class ApplicationRecord(WorkspaceModel):
    application_id: str
    job_id: str
    current_snapshot_id: str
    status: ApplicationStatus
    version: int
    next_action: str | None = None
    deadline_at: datetime | None = None
    applied_at: datetime | None = None
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class ApplicationArtifactRecord(WorkspaceModel):
    artifact_id: str
    workflow_mode: str = "single_custom"
    application_id: str
    artifact_type: ArtifactType
    version: int
    status: ArtifactStatus
    content: dict[str, Any]
    evidence_ids: list[str]
    verification_status: str | None = None
    created_by: str
    source_run_id: str | None = None
    source_session_id: str | None = None
    created_at: datetime


class ApplicationEventRecord(WorkspaceModel):
    event_id: str
    application_id: str
    sequence: int
    event_type: ApplicationEventType
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime


class TextArtifactContent(WorkspaceModel):
    text: str = Field(min_length=1, max_length=50_000)
