"""Minimal public Job Workspace routes."""

from fastapi import APIRouter, Depends, Query, status

from agent_runtime.workspace.repository import JobWorkspaceRepository
from agent_runtime.workspace.types import ApplicationStatus
from api.workspace_dependencies import get_workspace_repository
from api.workspace_schemas import (
    AnalyzeApplicationRequest, AnalyzeApplicationResponse, ApplicationArtifactsResponse,
    ApplicationDetail, ApplicationEventsResponse, ApplicationListResponse, ApplicationSummary,
    CreateApplicationRequest, CreateJobRequest, JobResponse, PublicApplication,
    PublicApplicationArtifact, PublicApplicationEvent, PublicJob, PublicJobSnapshot,
    SaveWorkspaceRequest, SaveWorkspaceResponse, TransitionApplicationRequest,
    UpdateApplicationRequest,
)
from api.routes.runs import get_run_service
from api.services.workspace_service import WorkspaceAnalysisService
from agent_runtime.workspace.types import ArtifactType

router = APIRouter(prefix="/api", tags=["job-workspace"])


@router.post("/workspaces", response_model=SaveWorkspaceResponse, status_code=status.HTTP_201_CREATED)
def save_workspace(request: SaveWorkspaceRequest,
                   repository: JobWorkspaceRepository = Depends(get_workspace_repository)):
    result = repository.save_workspace(**request.model_dump())
    return SaveWorkspaceResponse(job=_job(result.job), snapshot=_snapshot(result.snapshot),
        application=_application(result.application), created_job=result.created_job,
        created_snapshot=result.created_snapshot,
        created_application=result.created_application,
        duplicate_detected=result.duplicate_detected)


def _job(value) -> PublicJob:
    return PublicJob.model_validate(value.model_dump())


def _snapshot(value) -> PublicJobSnapshot:
    return PublicJobSnapshot.model_validate(value.model_dump(exclude={"raw_page_text"}))


def _application(value) -> PublicApplication:
    return PublicApplication.model_validate(value.model_dump())


@router.post("/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
def create_job(request: CreateJobRequest, repository: JobWorkspaceRepository = Depends(get_workspace_repository)):
    result = repository.create_or_find_job(**request.model_dump())
    return JobResponse(job=_job(result.job), snapshots=[_snapshot(result.snapshot)],
        created=result.created, snapshot_created=result.snapshot_created,
        duplicate_candidate_job_ids=result.duplicate_candidates)


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, repository: JobWorkspaceRepository = Depends(get_workspace_repository)):
    job, snapshots = repository.get_job(job_id)
    return JobResponse(job=_job(job), snapshots=[_snapshot(item) for item in snapshots])


@router.post("/applications", response_model=PublicApplication, status_code=status.HTTP_201_CREATED)
def create_application(request: CreateApplicationRequest, repository: JobWorkspaceRepository = Depends(get_workspace_repository)):
    return _application(repository.create_application(job_id=request.job_id, snapshot_id=request.snapshot_id))


@router.get("/applications", response_model=ApplicationListResponse)
def list_applications(status_filter: ApplicationStatus | None = Query(default=None, alias="status"),
                      company: str | None = None, search: str | None = None,
                      limit: int = Query(default=25, ge=1, le=100),
                      cursor: str | None = None,
                      repository: JobWorkspaceRepository = Depends(get_workspace_repository)):
    rows = repository.list_applications(status=status_filter, company=company,
        search=search, limit=limit + 1, cursor=cursor)
    more = len(rows) > limit; visible = rows[:limit]
    summaries = []
    for item in visible:
        job = repository.application_job(item.application_id)
        artifacts = repository.list_artifacts(item.application_id)
        match = next((entry for entry in reversed(artifacts)
                      if entry.artifact_type == ArtifactType.MATCH_REPORT), None)
        summaries.append(ApplicationSummary(**_application(item).model_dump(),
            company=job.company, title=job.title, location=job.location,
            match_score=match.content.get("overall_score") if match else None))
    return ApplicationListResponse(applications=summaries,
        next_cursor=visible[-1].application_id if more and visible else None)


@router.get("/applications/{application_id}", response_model=ApplicationDetail)
def get_application(application_id: str, repository: JobWorkspaceRepository = Depends(get_workspace_repository)):
    application = repository.get_application(application_id)
    job = repository.application_job(application_id)
    snapshots = repository.get_job(job.job_id)[1]
    snapshot = next(item for item in snapshots if item.snapshot_id == application.current_snapshot_id)
    artifacts = repository.list_artifacts(application_id)
    match = next((item for item in reversed(artifacts) if item.artifact_type == ArtifactType.MATCH_REPORT), None)
    resume = next((item for item in reversed(artifacts) if item.artifact_type == ArtifactType.TAILORED_RESUME), None)
    missing = []
    if match:
        missing = [*match.content.get("missing_required_requirements", []),
                   *match.content.get("missing_preferred_requirements", [])]
    return ApplicationDetail(**_application(application).model_dump(), job=_job(job),
        snapshot=_snapshot(snapshot), latest_match_score=(match.content.get("overall_score") if match else None),
        latest_missing_requirements=missing, latest_tailored_resume=resume.content if resume else None,
        associated_runs=repository.associated_runs(application_id))


@router.post("/applications/{application_id}/analyze", response_model=AnalyzeApplicationResponse)
async def analyze_application(application_id: str, request: AnalyzeApplicationRequest,
                              repository: JobWorkspaceRepository = Depends(get_workspace_repository),
                              run_service=Depends(get_run_service)):
    application, run_id, run_status, artifacts = await WorkspaceAnalysisService(
        repository, run_service).analyze(application_id, snapshot_id=request.snapshot_id,
        resume_text=request.resume_text, expected_version=request.expected_version)
    return AnalyzeApplicationResponse(application=_application(application), run_id=run_id,
        run_status=run_status, artifacts=[PublicApplicationArtifact.model_validate(
            item.model_dump(exclude={"application_id"})) for item in artifacts])


@router.patch("/applications/{application_id}/status", response_model=PublicApplication)
def transition_application(application_id: str, request: TransitionApplicationRequest,
                           repository: JobWorkspaceRepository = Depends(get_workspace_repository)):
    return _application(repository.transition_status(application_id,
        target_status=request.target_status, expected_version=request.expected_version,
        applied_at=request.applied_at))


@router.patch("/applications/{application_id}", response_model=PublicApplication)
def update_application(application_id: str, request: UpdateApplicationRequest,
                       repository: JobWorkspaceRepository = Depends(get_workspace_repository)):
    current = repository.get_application(application_id)
    update_action = "next_action" in request.model_fields_set and request.next_action != current.next_action
    update_deadline = "deadline_at" in request.model_fields_set and request.deadline_at != current.deadline_at
    return _application(repository.update_metadata(application_id,
        expected_version=request.expected_version, next_action=request.next_action,
        deadline_at=request.deadline_at, update_next_action=update_action,
        update_deadline=update_deadline))


@router.get("/applications/{application_id}/events", response_model=ApplicationEventsResponse)
def application_events(application_id: str, repository: JobWorkspaceRepository = Depends(get_workspace_repository)):
    return ApplicationEventsResponse(events=[PublicApplicationEvent.model_validate(
        item.model_dump(exclude={"application_id"})) for item in repository.list_events(application_id)])


@router.get("/applications/{application_id}/artifacts", response_model=ApplicationArtifactsResponse)
def application_artifacts(application_id: str, repository: JobWorkspaceRepository = Depends(get_workspace_repository)):
    return ApplicationArtifactsResponse(artifacts=[PublicApplicationArtifact.model_validate(
        item.model_dump(exclude={"application_id"})) for item in repository.list_artifacts(application_id)])
