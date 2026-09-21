"""Explicit worker registry; never imports a worker from model text."""
from __future__ import annotations

from agent_runtime.multi_agent.errors import TaskConflictError, UnknownWorkerError
from agent_runtime.multi_agent.types import AgentWorker


class AgentWorkerRegistry:
    def __init__(self) -> None:
        self._workers: dict[str, AgentWorker] = {}

    def register(self, worker: AgentWorker) -> None:
        name = worker.task_type
        if not name or name in self._workers:
            raise TaskConflictError("Worker task type is empty or already registered.")
        self._workers[name] = worker

    def get(self, task_type: str) -> AgentWorker:
        try:
            return self._workers[task_type]
        except KeyError as exc:
            raise UnknownWorkerError("The task type is not registered.") from exc

    def names(self) -> frozenset[str]:
        return frozenset(self._workers)

    def metadata(self) -> list[dict[str, str]]:
        return [{"task_type": name} for name in sorted(self._workers)]
