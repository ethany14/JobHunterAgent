"""FastAPI v0.3 application entry point."""

from typing import Literal

from fastapi import FastAPI

from api.routes.runs import router as runs_router

app = FastAPI(
    title="JobHunterAgent API",
    version="0.3.0",
    description="Evidence-grounded resume tailoring with human review.",
)
app.include_router(runs_router)


@app.get("/health", tags=["health"])
async def health() -> dict[str, Literal["ok"]]:
    return {"status": "ok"}
