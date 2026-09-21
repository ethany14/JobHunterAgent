"""Server-owned deterministic plan templates."""
from __future__ import annotations

from typing import Callable

from agent_runtime.multi_agent.errors import InvalidPlanError, TaskConflictError
from agent_runtime.multi_agent.types import AgentPlan


class AgentPlanTemplateRegistry:
    def __init__(self) -> None:
        self._templates: dict[str, Callable[[str, str], AgentPlan]] = {}

    def register(self, template_id: str, factory: Callable[[str, str], AgentPlan]) -> None:
        if template_id in self._templates:
            raise TaskConflictError("Plan template already exists.")
        self._templates[template_id] = factory

    def build(self, template_id: str, parent_session_id: str, idempotency_key: str) -> AgentPlan:
        try:
            factory = self._templates[template_id]
        except KeyError as exc:
            raise InvalidPlanError("Plan template is not approved on this server.") from exc
        plan = factory(parent_session_id, idempotency_key)
        if plan.template_id != template_id or plan.parent_session_id != parent_session_id:
            raise InvalidPlanError("Plan template returned an invalid scope.")
        return plan

    def names(self) -> frozenset[str]:
        return frozenset(self._templates)
