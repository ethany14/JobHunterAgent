"""Public Job Workspace API contracts."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.workspace.types import ApplicationEventType, ApplicationStatus, ArtifactStatus, ArtifactType


class WorkspaceApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateJobRequest(WorkspaceApiModel):
    canonical_url: str | None = Field(default=None, max_length=2048)
    source_site: str | None = Field(default=None, max_length=128)
    company: str | None = Field(default=None, max_length=256)
    title: str | None = Field(default=None, max_length=256)
    location: str | None = Field(default=None, max_length=256)
    raw_page_text: str | None = Field(default=None, max_length=50_000)
    cleaned_job_description: str = Field(min_length=1, max_length=50_000)
    extraction_metadata: dict[str, Any] = Field(default_factory=dict)


class PublicJob(WorkspaceApiModel):
    job_id: str
    canonical_url: str | None
    source_site: str | None
    company: str | None
    title: str | None
    location: str | None
    created_at: datetime
    updated_at: datetime


class PublicJobSnapshot(WorkspaceApiModel):
    snapshot_id: str
    job_id: str
    content_hash: str
    cleaned_job_description: str
    extraction_metadata: dict[str, Any]
    captured_at: datetime


class JobResponse(WorkspaceApiModel):
    job: PublicJob
    snapshots: list[PublicJobSnapshot]
    created: bool | None = None
    snapshot_created: bool | None = None
    duplicate_candidate_job_ids: list[str] = Field(default_factory=list)


class CreateApplicationRequest(WorkspaceApiModel):
    job_id: str
    snapshot_id: str


class SaveWorkspaceRequest(WorkspaceApiModel):
    source_url: str | None = Field(default=None, max_length=2048)
    source_site: str | None = Field(default=None, max_length=128)
    company: str | None = Field(default=None, max_length=256)
    title: str | None = Field(default=None, max_length=256)
    location: str | None = Field(default=None, max_length=256)
    raw_page_text: str | None = Field(default=None, max_length=50_000)
    cleaned_job_description: str = Field(min_length=1, max_length=50_000)
    extraction_metadata: dict[str, Any] = Field(default_factory=dict)
    reopen_existing: bool = True


class SaveWorkspaceResponse(WorkspaceApiModel):
    job: PublicJob
    snapshot: PublicJobSnapshot
    application: "PublicApplication"
    created_job: bool
    created_snapshot: bool
    created_application: bool
    duplicate_detected: bool


class PublicApplication(WorkspaceApiModel):
    application_id: str
    job_id: str
    current_snapshot_id: str
    status: ApplicationStatus
    version: int
    next_action: str | None
    deadline_at: datetime | None
    applied_at: datetime | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class ApplicationSummary(PublicApplication):
    company: str | None
    title: str | None
    location: str | None
    match_score: float | None = None


class ApplicationDetail(PublicApplication):
    job: PublicJob
    snapshot: PublicJobSnapshot
    latest_match_score: float | None = None
    latest_missing_requirements: list[dict[str, Any]] = Field(default_factory=list)
    latest_tailored_resume: dict[str, Any] | None = None
    associated_runs: list[dict[str, Any]] = Field(default_factory=list)


class ApplicationListResponse(WorkspaceApiModel):
    applications: list[ApplicationSummary]
    next_cursor: str | None = None


class TransitionApplicationRequest(WorkspaceApiModel):
    target_status: ApplicationStatus
    expected_version: int = Field(ge=0)
    applied_at: datetime | None = None


class UpdateApplicationRequest(WorkspaceApiModel):
    expected_version: int = Field(ge=0)
    next_action: str | None = Field(default=None, max_length=2000)
    deadline_at: datetime | None = None


class PublicApplicationEvent(WorkspaceApiModel):
    event_id: str
    sequence: int
    event_type: ApplicationEventType
    payload: dict[str, Any]
    occurred_at: datetime


class ApplicationEventsResponse(WorkspaceApiModel):
    events: list[PublicApplicationEvent]


class PublicApplicationArtifact(WorkspaceApiModel):
    artifact_id: str
    workflow_mode: Literal["single_custom", "multi_agent_v1"] = "single_custom"
    artifact_type: ArtifactType
    version: int
    status: ArtifactStatus
    content: dict[str, Any]
    evidence_ids: list[str]
    verification_status: str | None
    created_by: str
    source_run_id: str | None
    source_session_id: str | None
    created_at: datetime


class ApplicationArtifactsResponse(WorkspaceApiModel):
    artifacts: list[PublicApplicationArtifact]


class AnalyzeApplicationRequest(WorkspaceApiModel):
    snapshot_id: str
    resume_text: str = Field(min_length=1, max_length=50_000)
    expected_version: int = Field(ge=0)


class AnalyzeApplicationResponse(WorkspaceApiModel):
    application: PublicApplication
    run_id: str | None
    run_status: str
    artifacts: list[PublicApplicationArtifact] = Field(default_factory=list)
