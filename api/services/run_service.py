"""In-process orchestration for synchronous v0.3 API runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from langgraph.types import Command

from api.schemas.runs import CreateRunRequest, CreateRunResponse, ReviewRequest, RunResponse, RunStatus
from job_agent.agent import graph as default_graph
from job_agent.results import interrupt_payload, public_result


class RunNotFoundError(LookupError):
    pass


class InvalidRunStateError(RuntimeError):
    pass


@dataclass
class RunRecord:
    run_id: str
    status: RunStatus = "running"
    result: dict[str, Any] | None = None
    error: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class RunService:
    """Manage graph runs and checkpoints for one API process."""

    def __init__(self, graph: Any = default_graph) -> None:
        self._graph = graph
        self._runs: dict[str, RunRecord] = {}

    async def create_run(self, request: CreateRunRequest) -> CreateRunResponse:
        run_id = str(uuid4())
        record = RunRecord(run_id=run_id)
        self._runs[run_id] = record
        config = self._config(run_id)
        try:
            state = await asyncio.to_thread(
                self._graph.invoke,
                {
                    "resume_text": request.resume_text,
                    "job_description": request.job_description,
                },
                config=config,
            )
            self._apply_graph_state(record, state)
        except Exception as exc:
            self._mark_failed(record, exc)
        return CreateRunResponse(run_id=run_id, status=record.status)

    async def get_run(self, run_id: str) -> RunResponse:
        return self._response(self._get_record(run_id))

    async def review_run(self, run_id: str, request: ReviewRequest) -> RunResponse:
        record = self._get_record(run_id)
        async with record.lock:
            if record.status != "awaiting_review":
                raise InvalidRunStateError(
                    f"Run '{run_id}' is not awaiting review; current status is "
                    f"'{record.status}'."
                )
            record.status = "running" if request.approved else "revising"
            try:
                state = await asyncio.to_thread(
                    self._graph.invoke,
                    Command(
                        resume={
                            "approved": request.approved,
                            "feedback": request.feedback,
                        }
                    ),
                    config=self._config(run_id),
                )
                self._apply_graph_state(record, state)
            except Exception as exc:
                self._mark_failed(record, exc)
            return self._response(record)

    @staticmethod
    def _config(run_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": run_id}}

    def _get_record(self, run_id: str) -> RunRecord:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise RunNotFoundError(f"Run '{run_id}' was not found.") from exc

    @staticmethod
    def _mark_failed(record: RunRecord, exc: Exception) -> None:
        record.status = "failed"
        record.error = str(exc)

    @staticmethod
    def _apply_graph_state(record: RunRecord, state: dict[str, Any]) -> None:
        record.result = public_result(state)
        record.error = None
        if interrupt_payload(state) is not None:
            record.status = "awaiting_review"
        elif state.get("approved") or state.get("workflow_status") == "approved":
            record.status = "approved"
        else:
            raise RuntimeError("Graph stopped without approval or a human-review interrupt.")

    @staticmethod
    def _response(record: RunRecord) -> RunResponse:
        return RunResponse(
            run_id=record.run_id,
            status=record.status,
            result=record.result,
            error=record.error,
        )
