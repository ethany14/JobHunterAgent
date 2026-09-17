"""FastAPI v0.4 application entry point."""

from typing import Literal

from fastapi import FastAPI

from api.routes.runs import router as runs_router
from api.services.run_service import RunService


def create_app(run_service: RunService | None = None) -> FastAPI:
    application = FastAPI(
        title="JobHunterAgent API",
        version="0.4.0",
        description="Evidence-grounded resume tailoring with durable human review.",
    )
    application.state.run_service = run_service
    application.include_router(runs_router)

    @application.get("/health", tags=["health"])
    async def health() -> dict[str, Literal["ok"]]:
        return {"status": "ok"}

    return application


app = create_app()
