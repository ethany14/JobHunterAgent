"""Orchestration for persisted synchronous API runs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from langgraph.types import Command

from api.repositories.run_repository import RunRecord, RunRepository
from api.schemas.runs import CreateRunRequest, CreateRunResponse, ReviewRequest, RunResponse
from job_agent.results import interrupt_payload, public_result

logger = logging.getLogger(__name__)
SAFE_RUN_ERROR = "The agent could not complete this run. Please try again."


class RunNotFoundError(LookupError):
    pass


class InvalidRunStateError(RuntimeError):
    pass


class RunService:
    """Coordinate repository state with a checkpointed LangGraph graph."""

    def __init__(
        self,
        *,
        repository: RunRepository,
        graph: Any,
        result_serializer: Callable[[dict[str, Any]], dict[str, Any]] = public_result,
        resources: tuple[Any, ...] = (),
    ) -> None:
        self._repository = repository
        self._graph = graph
        self._result_serializer = result_serializer
        self._resources = resources
        self._locks: dict[str, asyncio.Lock] = {}

    async def create_run(self, request: CreateRunRequest) -> CreateRunResponse:
        run_id = str(uuid4())
        self._repository.create(
            run_id=run_id,
            thread_id=run_id,
            resume_text=request.resume_text,
            job_description=request.job_description,
        )
        try:
            state = await asyncio.to_thread(
                self._graph.invoke,
                {
                    "resume_text": request.resume_text,
                    "job_description": request.job_description,
                },
                config=self._config(run_id),
            )
            record = self._store_graph_state(run_id, state)
        except Exception as exc:
            record = self._mark_failed(run_id, exc)
        return CreateRunResponse(run_id=run_id, status=record.status)

    async def get_run(self, run_id: str) -> RunResponse:
        return self._response(self._get_record(run_id))

    async def review_run(self, run_id: str, request: ReviewRequest) -> RunResponse:
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            record = self._get_record(run_id)
            processing_status = "running" if request.approved else "revising"
            claimed = self._repository.transition_status(
                run_id,
                expected="awaiting_review",
                new_status=processing_status,
            )
            if not claimed:
                current = self._get_record(run_id)
                raise InvalidRunStateError(
                    f"Run '{run_id}' is not awaiting review; current status is "
                    f"'{current.status}'."
                )
            try:
                state = await asyncio.to_thread(
                    self._graph.invoke,
                    Command(
                        resume={
                            "approved": request.approved,
                            "feedback": request.feedback,
                        }
                    ),
                    config=self._config(record.thread_id),
                )
                updated = self._store_graph_state(run_id, state)
            except Exception as exc:
                updated = self._mark_failed(run_id, exc)
            return self._response(updated)

    def close(self) -> None:
        for resource in reversed(self._resources):
            close = getattr(resource, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _config(thread_id: str) -> dict[str, dict[str, str]]:
        return {"configurable": {"thread_id": thread_id}}

    def _get_record(self, run_id: str) -> RunRecord:
        record = self._repository.get(run_id)
        if record is None:
            raise RunNotFoundError(f"Run '{run_id}' was not found.")
        return record

    def _mark_failed(self, run_id: str, exc: Exception) -> RunRecord:
        logger.exception("Agent run %s failed", run_id, exc_info=exc)
        record = self._repository.update(
            run_id,
            status="failed",
            result=None,
            error_message=SAFE_RUN_ERROR,
        )
        if record is None:
            raise RunNotFoundError(f"Run '{run_id}' was not found.") from exc
        return record

    def _store_graph_state(self, run_id: str, state: dict[str, Any]) -> RunRecord:
        result = self._result_serializer(state)
        if interrupt_payload(state) is not None:
            status = "awaiting_review"
        elif state.get("approved") or state.get("workflow_status") == "approved":
            status = "approved"
        else:
            raise RuntimeError("Graph stopped without approval or a human-review interrupt.")
        record = self._repository.update(
            run_id,
            status=status,
            result=result,
            error_message=None,
        )
        if record is None:
            raise RunNotFoundError(f"Run '{run_id}' was not found.")
        return record

    @staticmethod
    def _response(record: RunRecord) -> RunResponse:
        return RunResponse(
            run_id=record.run_id,
            status=record.status,
            result=record.result,
            error=record.error_message,
        )
