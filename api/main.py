"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from collections.abc import Callable
from typing import Any

import anyio
from fastapi import FastAPI

from api.error_handlers import install_error_handlers
from api.context_routes import router as context_router
from api.routes.runs import router as runs_router
from api.session_dependencies import SessionRuntime, create_session_runtime
from api.session_routes import router as sessions_router
from api.workspace_routes import router as workspace_router
from api.services.run_service import BackendRoutingRunService, RunService


def create_app(
    run_service: RunService | BackendRoutingRunService | None = None,
    *,
    session_runtime: SessionRuntime | None = None,
    session_runtime_factory: Callable[[], SessionRuntime] | None = None,
) -> FastAPI:
    factory = session_runtime_factory
    if factory is None and run_service is None and session_runtime is None:
        factory = create_session_runtime

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        created_runtime = None
        if (
            application.state.session_runtime is None
            and factory is not None
            and not application.dependency_overrides
        ):
            created_runtime = await anyio.to_thread.run_sync(factory)
            application.state.session_runtime = created_runtime
        try:
            yield
        finally:
            if created_runtime is not None:
                await anyio.to_thread.run_sync(created_runtime.close)
                application.state.session_runtime = None

    application = FastAPI(
        title="JobHunterAgent API",
        version="0.5.0",
        description=(
            "Evidence-grounded resume tailoring and persistent local agent sessions. "
            "Session access currently assumes a trusted single-user local deployment."
        ),
        lifespan=lifespan,
    )
    application.state.run_service = run_service
    application.state.session_runtime = session_runtime
    application.include_router(runs_router)
    application.include_router(sessions_router)
    application.include_router(context_router)
    application.include_router(workspace_router)
    install_error_handlers(application)

    @application.get("/health", tags=["health"])
    async def health() -> dict[str, Any]:
        runtime = application.state.session_runtime
        mcp = (
            runtime.mcp_health().model_dump(mode="json")
            if runtime is not None
            else {
                "configured_servers": 0,
                "ready_servers": 0,
                "failed_optional_servers": 0,
                "registered_tools": 0,
                "servers": [],
            }
        )
        return {
            "status": "degraded" if mcp["failed_optional_servers"] else "ok",
            "mcp": mcp,
        }

    return application


app = create_app()
