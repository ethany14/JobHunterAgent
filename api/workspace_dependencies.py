from fastapi import Request

from agent_runtime.workspace.repository import JobWorkspaceRepository


def get_workspace_repository(request: Request) -> JobWorkspaceRepository:
    runtime = getattr(request.app.state, "session_runtime", None)
    if runtime is None or runtime.workspace is None:
        raise RuntimeError("The Job Workspace runtime is not initialized.")
    return runtime.workspace
