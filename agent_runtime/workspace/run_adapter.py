"""Focused adapter from existing completed run projections into Workspace artifacts."""

from __future__ import annotations

from agent_runtime.workspace.errors import ArtifactValidationError
from agent_runtime.workspace.repository import JobWorkspaceRepository
from agent_runtime.workspace.types import ArtifactStatus, ArtifactType
from api.repositories.run_repository import RunRepository


class WorkspaceRunAdapter:
    def __init__(self, workspace: JobWorkspaceRepository, runs: RunRepository) -> None:
        self._workspace = workspace
        self._runs = runs

    def attach_completed_run(self, application_id: str, run_id: str, *, expected_version: int):
        run = self._runs.get(run_id)
        if run is None or run.result is None:
            raise ArtifactValidationError("A completed public run result is required.")
        state = self._workspace.attach_run(application_id, run_id,
            role="resume_analysis", expected_version=expected_version)
        mappings = (
            (ArtifactType.JOB_ANALYSIS, "job_analysis"),
            (ArtifactType.MATCH_REPORT, "skill_match"),
            (ArtifactType.TAILORED_RESUME, "tailored_resume"),
        )
        artifacts = []
        for artifact_type, key in mappings:
            content = run.result.get(key)
            if content is None:
                continue
            artifact = self._workspace.create_artifact(application_id,
                artifact_type=artifact_type, content=content,
                evidence_ids=self._evidence_ids(content), created_by="run_adapter",
                expected_version=state.version, status=ArtifactStatus.VERIFIED,
                verification_status="verified", source_run_id=run_id)
            artifacts.append(artifact)
            state = self._workspace.get_application(application_id)
        return state, artifacts

    @staticmethod
    def _evidence_ids(content: dict) -> list[str]:
        found: set[str] = set()
        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "evidence_ids" and isinstance(item, list):
                        found.update(str(entry) for entry in item)
                    else: visit(item)
            elif isinstance(value, list):
                for item in value: visit(item)
        visit(content)
        return sorted(found)
