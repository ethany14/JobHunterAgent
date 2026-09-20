"""Integration boundary between Workspace and the existing Run workflow."""

from __future__ import annotations

from agent_runtime.workspace.repository import JobWorkspaceRepository
from agent_runtime.workspace.run_adapter import WorkspaceRunAdapter
from agent_runtime.workspace.types import ApplicationStatus
from api.repositories.run_repository import RunRepository
from api.schemas.runs import CreateRunRequest


class WorkspaceAnalysisService:
    def __init__(self, workspace: JobWorkspaceRepository, run_service) -> None:
        self._workspace = workspace
        self._run_service = run_service
        self._adapter = WorkspaceRunAdapter(
            workspace, RunRepository(workspace.session_factory)
        )

    async def analyze(self, application_id: str, *, snapshot_id: str,
                      resume_text: str, expected_version: int):
        current = self._workspace.require_snapshot_for_application(application_id, snapshot_id)
        if current.version != expected_version:
            from agent_runtime.workspace.errors import StaleApplicationError
            raise StaleApplicationError("The application version is stale.")
        analyzing = self._workspace.transition_status(application_id,
            target_status=ApplicationStatus.ANALYZING, expected_version=expected_version)
        run_id = None
        try:
            snapshot = self._workspace.current_snapshot(application_id)
            created = await self._run_service.create_run(CreateRunRequest(
                resume_text=resume_text,
                job_description=snapshot.cleaned_job_description,
            ))
            run_id = created.run_id
            state = self._workspace.attach_run(application_id, run_id,
                role="resume_analysis", expected_version=analyzing.version)
            if created.status == "failed":
                failed = self._workspace.transition_status(application_id,
                    target_status=ApplicationStatus.ANALYSIS_FAILED,
                    expected_version=state.version, error_code="analysis_run_failed")
                failed = self._workspace.update_next_action(application_id,
                    next_action="Retry resume analysis", expected_version=failed.version)
                return failed, run_id, created.status, []
            if created.status != "awaiting_review":
                return state, run_id, created.status, []
            state, artifacts = self._adapter.attach_completed_run(
                application_id, run_id, expected_version=state.version
            )
            ready = self._workspace.transition_status(application_id,
                target_status=ApplicationStatus.MATERIALS_READY,
                expected_version=state.version)
            return ready, run_id, created.status, artifacts
        except Exception:
            latest = self._workspace.get_application(application_id)
            if latest.status == ApplicationStatus.ANALYZING:
                self._workspace.transition_status(application_id,
                    target_status=ApplicationStatus.ANALYSIS_FAILED,
                    expected_version=latest.version, error_code="analysis_failed")
            raise
