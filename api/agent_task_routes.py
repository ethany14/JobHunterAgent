"""Read-focused, template-restricted task APIs."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from agent_runtime.multi_agent.errors import TaskConflictError
from agent_runtime.multi_agent.policy import AgentPlanValidator
from agent_runtime.multi_agent.types import AgentTask
from agent_runtime.skills.types import SkillStatus
from api.agent_task_schemas import (
    CreatePlanRequest, PublicArtifactLink, PublicTask, PublicTaskEvent, TaskMutationRequest,
)
from api.session_dependencies import SessionRuntime, get_session_runtime

router = APIRouter(prefix="/api", tags=["agent-tasks"])


def _runtime(runtime: SessionRuntime):
    if runtime.agent_tasks is None or runtime.agent_workers is None or runtime.agent_plan_templates is None:
        raise TaskConflictError("Task runtime is unavailable.")
    return runtime.agent_tasks


def _public(task: AgentTask, runtime: SessionRuntime) -> PublicTask:
    return PublicTask(task_id=task.task_id,
        workflow_mode=runtime.agent_tasks.plan_mode(task.root_task_id),
        parent_task_id=task.parent_task_id,
        root_task_id=task.root_task_id, task_type=task.task_type,
        agent_role=task.agent_role, status=task.status.value, priority=task.priority,
        depth=task.depth, version=task.version, attempt_count=task.attempt_count,
        max_attempts=task.max_attempts,
        result_summary=task.result_summary, error_code=task.error_code,
        output_artifacts=[link for link in runtime.agent_tasks.artifact_links(task.task_id)
            if link["direction"] == "output"],
        created_at=task.created_at, started_at=task.started_at,
        completed_at=task.completed_at,
        dependencies=runtime.agent_tasks.dependencies(task.task_id))


@router.post("/agent-plans", response_model=PublicTask)
def create_plan(request: CreatePlanRequest, runtime: SessionRuntime = Depends(get_session_runtime)):
    tasks = _runtime(runtime)
    parent = runtime.sessions.require(request.parent_session_id)
    if parent.version != request.expected_session_version:
        raise TaskConflictError("Parent Session version changed.")
    plan = runtime.agent_plan_templates.build(request.template_id,
        request.parent_session_id, request.idempotency_key)
    allowed_artifacts = frozenset(artifact_id for spec in plan.tasks
        for artifact_id in spec.input_artifact_ids
        if tasks.artifact_exists(artifact_id, application_id=spec.application_id,
            parent_session_id=plan.parent_session_id))
    validator = AgentPlanValidator(worker_types=runtime.agent_workers.names(),
        registered_tools=runtime.registry.names(), parent_tools=parent.allowed_tools,
        session_tools=parent.allowed_tools,
        active_skill_version_ids=frozenset(record.version_id for record in
            runtime.skills.discover() if record.status == SkillStatus.ACTIVE)
            if runtime.skills is not None else frozenset(),
        valid_artifact_ids=allowed_artifacts,
        task_type_tools=runtime.agent_scheduler.contexts.task_type_tools
            if runtime.agent_scheduler is not None else {})
    depths = validator.validate(plan)
    return _public(tasks.create_plan(plan, depths), runtime)


@router.get("/agent-tasks/{task_id}", response_model=PublicTask)
def get_task(task_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    return _public(_runtime(runtime).require(task_id), runtime)


@router.get("/applications/{application_id}/agent-tasks", response_model=list[PublicTask])
def application_tasks(application_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    if runtime.workspace is None:
        raise TaskConflictError("Workspace is unavailable.")
    runtime.workspace.get_application(application_id)
    return [_public(item, runtime) for item in _runtime(runtime).for_application(application_id)]


@router.get("/agent-tasks/{task_id}/children", response_model=list[PublicTask])
def children(task_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    tasks = _runtime(runtime)
    tasks.require(task_id)
    return [_public(item, runtime) for item in tasks.children(task_id)]


@router.get("/agent-tasks/{task_id}/events", response_model=list[PublicTaskEvent])
def events(task_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    return [PublicTaskEvent(event_id=item.event_id, sequence=item.sequence,
        event_type=item.event_type.value, attempt_id=item.attempt_id,
        occurred_at=item.occurred_at) for item in _runtime(runtime).events(task_id)]


@router.get("/agent-tasks/{task_id}/artifacts", response_model=list[PublicArtifactLink])
def artifacts(task_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    return [PublicArtifactLink.model_validate(item) for item in _runtime(runtime).artifact_links(task_id)]


@router.post("/agent-tasks/{task_id}/cancel", response_model=PublicTask)
def cancel(task_id: str, request: TaskMutationRequest,
           runtime: SessionRuntime = Depends(get_session_runtime)):
    tasks = _runtime(runtime)
    return _public(tasks.cancel(task_id, expected_version=request.expected_version,
        subtree=request.subtree), runtime)


@router.post("/agent-tasks/{task_id}/retry", response_model=PublicTask)
def retry(task_id: str, request: TaskMutationRequest,
          runtime: SessionRuntime = Depends(get_session_runtime)):
    return _public(_runtime(runtime).retry(task_id, expected_version=request.expected_version), runtime)


@router.post("/agent-tasks/{task_id}/resume", response_model=PublicTask)
def resume(task_id: str, request: TaskMutationRequest,
           runtime: SessionRuntime = Depends(get_session_runtime)):
    return _public(_runtime(runtime).resume(task_id, expected_version=request.expected_version), runtime)
