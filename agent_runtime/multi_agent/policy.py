"""Pure plan validation and scoped context selection."""
from __future__ import annotations

from collections import defaultdict

from agent_runtime.multi_agent.errors import InvalidPlanError
from agent_runtime.multi_agent.types import AgentPlan, DependencyType, TaskStatus


class AgentPlanValidator:
    def __init__(self, *, worker_types: frozenset[str], registered_tools: frozenset[str],
                 parent_tools: frozenset[str], session_tools: frozenset[str],
                 active_skill_version_ids: frozenset[str] = frozenset(),
                 valid_artifact_ids: frozenset[str] = frozenset(),
                 task_type_tools: dict[str, frozenset[str]] | None = None) -> None:
        self.worker_types = worker_types
        self.registered_tools = registered_tools
        self.parent_tools = parent_tools
        self.session_tools = session_tools
        self.active_skill_version_ids = active_skill_version_ids
        self.valid_artifact_ids = valid_artifact_ids
        self.task_type_tools = task_type_tools

    def validate(self, plan: AgentPlan) -> dict[str, int]:
        by_key = {task.key: task for task in plan.tasks}
        if len(by_key) != len(plan.tasks) or plan.root_key not in by_key:
            raise InvalidPlanError("Plan task keys must be unique and include the root.")
        if len(plan.tasks) > plan.budget.max_tasks:
            raise InvalidPlanError("Plan task budget exceeded.")
        edges: dict[str, set[str]] = defaultdict(set)
        child_counts: dict[str, int] = defaultdict(int)
        for task in plan.tasks:
            if task.task_type not in self.worker_types:
                raise InvalidPlanError("Plan contains an unregistered task type.")
            if task.allowed_tools - (self.registered_tools & self.parent_tools & self.session_tools):
                raise InvalidPlanError("Task requests a tool outside its allowed intersection.")
            if self.task_type_tools is not None and task.allowed_tools - self.task_type_tools.get(task.task_type, frozenset()):
                raise InvalidPlanError("Task requests a tool disallowed for its worker type.")
            if task.allowed_skill_version_ids - self.active_skill_version_ids:
                raise InvalidPlanError("Task requests an inactive or unallowed Skill version.")
            if set(task.input_artifact_ids) - self.valid_artifact_ids:
                raise InvalidPlanError("Plan references an unavailable input artifact.")
            if task.parent_key is not None:
                if task.parent_key not in by_key or task.key == plan.root_key:
                    raise InvalidPlanError("Task parent is invalid.")
                edges[task.key].add(task.parent_key)
                child_counts[task.parent_key] += 1
            elif task.key != plan.root_key:
                raise InvalidPlanError("Every non-root task requires a parent.")
            for source_key in task.input_from_tasks.values():
                if source_key not in by_key or source_key == task.key:
                    raise InvalidPlanError("Input artifact mapping names an invalid source task.")
                if not any(dep.task_key == task.key and dep.depends_on_key == source_key
                           and dep.dependency_type == DependencyType.REQUIRES_SUCCESS
                           for dep in plan.dependencies):
                    raise InvalidPlanError("Mapped task output requires a success dependency.")
            for source_key in task.optional_input_from_tasks.values():
                if source_key not in by_key or source_key == task.key:
                    raise InvalidPlanError("Optional input mapping names an invalid source task.")
                if not any(dep.task_key == task.key and dep.depends_on_key == source_key
                           and dep.dependency_type == DependencyType.REQUIRES_COMPLETION
                           for dep in plan.dependencies):
                    raise InvalidPlanError("Optional mapped output requires a completion dependency.")
        if by_key[plan.root_key].parent_key is not None:
            raise InvalidPlanError("Root task cannot have a parent task.")
        if any(count > plan.budget.max_children_per_task for count in child_counts.values()):
            raise InvalidPlanError("Plan child count budget exceeded.")
        for dep in plan.dependencies:
            if dep.task_key not in by_key or dep.depends_on_key not in by_key or dep.task_key == dep.depends_on_key:
                raise InvalidPlanError("Dependency endpoints are invalid.")
            if dep.task_key == plan.root_key:
                raise InvalidPlanError("Root task cannot depend on a child task.")
            edges[dep.task_key].add(dep.depends_on_key)
        visiting: set[str] = set()
        depths: dict[str, int] = {}
        def depth(key: str) -> int:
            if key in visiting:
                raise InvalidPlanError("Plan contains a dependency cycle.")
            if key in depths:
                return depths[key]
            visiting.add(key)
            value = 0 if key == plan.root_key else 1 + max((depth(parent) for parent in edges[key]), default=0)
            visiting.remove(key)
            depths[key] = value
            return value
        for key in by_key:
            depth(key)
        if max(depths.values()) > plan.budget.max_depth:
            raise InvalidPlanError("Plan depth budget exceeded.")
        return depths


def dependency_ready(dependencies: list[tuple[DependencyType, TaskStatus]]) -> tuple[bool, bool]:
    """Return (ready, permanently_blocked) for persisted dependency states."""
    for kind, status in dependencies:
        if kind == DependencyType.REQUIRES_SUCCESS and status in {
            TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT,
        }:
            return False, True
        if kind == DependencyType.REQUIRES_SUCCESS and status != TaskStatus.SUCCEEDED:
            return False, False
        if kind == DependencyType.REQUIRES_COMPLETION and status not in {
            TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT,
        }:
            return False, False
    return True, False
