"""Analysis-only resume/JD matching without draft generation or review state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from time import perf_counter
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from custom_agent.handlers import JobAgentStepHandler
from custom_agent.state import AgentState, Step
from job_agent.schemas import JobAnalysis, ResumeAnalysis, SkillMatch


class FitAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: str
    status: str = "completed"
    resume_analysis: ResumeAnalysis
    job_analysis: JobAnalysis
    skill_match: SkillMatch
    model_calls: int = Field(default=3, ge=0)
    latency_seconds: float = Field(ge=0)


class FitAnalysisService:
    """Run the shared deterministic pipeline only through requirement matching."""

    _STEPS = (
        Step.VALIDATE_INPUT,
        Step.ANALYZE_RESUME,
        Step.VALIDATE_EVIDENCE,
        Step.ANALYZE_JOB,
        Step.MATCH_SKILLS,
    )

    def __init__(
        self,
        handler: JobAgentStepHandler | None = None,
        *,
        timer: Callable[[], float] = perf_counter,
    ) -> None:
        self._handler = handler or JobAgentStepHandler()
        self._timer = timer

    async def analyze(self, *, resume_text: str, job_description: str) -> FitAnalysisResult:
        return await asyncio.to_thread(
            self._analyze_sync,
            resume_text=resume_text,
            job_description=job_description,
        )

    def _analyze_sync(self, *, resume_text: str, job_description: str) -> FitAnalysisResult:
        started = self._timer()
        state = AgentState(
            run_id=f"analysis-{uuid4()}",
            resume_text=resume_text,
            job_description=job_description,
        )
        for step in self._STEPS:
            state = AgentState.model_validate({
                **state.model_dump(mode="python"),
                "step": step,
            })
            outcome = self._handler.execute(step, state)
            state = AgentState.model_validate({
                **state.model_dump(mode="python"),
                **outcome.updates,
            })
        if state.resume_analysis is None or state.job_analysis is None or state.skill_match is None:
            raise RuntimeError("Fit analysis completed without the required outputs.")
        return FitAnalysisResult(
            analysis_id=state.run_id,
            resume_analysis=state.resume_analysis,
            job_analysis=state.job_analysis,
            skill_match=state.skill_match,
            latency_seconds=max(0, self._timer() - started),
        )
