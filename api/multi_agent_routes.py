"""Opt-in, server-defined Job workflow APIs; Standard remains the default."""
from __future__ import annotations

import hashlib
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, HTTPException

from agent_runtime.job_workflow.plan import build_pack_plan
from agent_runtime.application_pack.errors import PackNotFoundError
from agent_runtime.multi_agent.errors import TaskConflictError, TaskNotFoundError
from agent_runtime.multi_agent.policy import AgentPlanValidator
from agent_runtime.multi_agent.types import MultiAgentBudget, TaskStatus
from agent_runtime.security import canonical_json
from agent_runtime.sessions.errors import SessionAlreadyExistsError
from agent_runtime.sessions.events import SessionEvent, SessionEventType
from agent_runtime.sessions.state import SessionState
from api.agent_task_routes import _public
from api.multi_agent_schemas import MultiRunMutation, MultiRunRequest, MultiRunResume
from api.session_dependencies import SessionRuntime, get_session_runtime

router = APIRouter(prefix="/api", tags=["multi-agent-job-workflow"])


def _require_root(root_id: str, runtime: SessionRuntime):
    if runtime.agent_tasks is None:
        raise TaskConflictError("Task runtime is unavailable.")
    root = runtime.agent_tasks.require(root_id)
    if root.parent_task_id is not None or runtime.agent_tasks.plan_mode(root_id) != "multi_agent_v1":
        raise TaskNotFoundError("Multi-Agent execution does not exist.")
    return root


def _summary(root_id: str, runtime: SessionRuntime) -> dict:
    root = _require_root(root_id, runtime)
    children = runtime.agent_tasks.children(root_id)
    all_tasks = [root, *children]
    statuses = {task.status for task in all_tasks}
    assembled = next((task for task in all_tasks if task.task_type == "pack_assembler"), None)
    if assembled is not None and assembled.status == TaskStatus.SUCCEEDED:
        status = "awaiting_review"
    elif TaskStatus.AWAITING_INPUT in statuses:
        status = "awaiting_input"
    elif TaskStatus.AWAITING_APPROVAL in statuses:
        status = "awaiting_approval"
    elif TaskStatus.FAILED in statuses:
        status = "failed"
    elif TaskStatus.CANCEL_REQUESTED in statuses or TaskStatus.CANCELLED in statuses:
        status = "cancelled"
    elif TaskStatus.TIMED_OUT in statuses:
        status = "timed_out"
    elif statuses == {TaskStatus.SUCCEEDED}:
        status = "awaiting_review"
    else:
        status = "running"
    pack_id = str(uuid5(NAMESPACE_URL, f"pack:{root_id}"))
    try:
        pack = runtime.packs.get(pack_id) if runtime.packs is not None else None
    except PackNotFoundError:
        pack = None
    usage: dict[str, float] = {}
    for task in all_tasks:
        for key, value in (task.result_summary or {}).get("usage", {}).items():
            if isinstance(value, (int, float)):
                usage[key] = usage.get(key, 0) + value
    return {"root_task_id": root_id, "application_id": root.application_id,
        "workflow_mode": "multi_agent_v1", "status": status,
        "pack_id": pack.pack_id if pack else None,
        "pack_status": pack.status.value if pack else None,
        "task_count": len(all_tasks), "usage": usage,
        "action_required": [task.task_id for task in all_tasks
            if task.status in {TaskStatus.AWAITING_INPUT, TaskStatus.AWAITING_APPROVAL}]}


@router.post("/applications/{application_id}/multi-agent-runs")
def create_multi_run(application_id: str, body: MultiRunRequest,
                     runtime: SessionRuntime = Depends(get_session_runtime)):
    if any(value is None for value in (runtime.workspace, runtime.agent_tasks,
                                      runtime.agent_workers, runtime.agent_scheduler,
                                      runtime.packs)):
        raise TaskConflictError("Multi-Agent runtime is unavailable.")
    application = runtime.workspace.get_application(application_id)
    payload = body.model_dump(mode="json", exclude={"expected_application_version", "idempotency_key"})
    request_hash = hashlib.sha256(canonical_json({"application_id": application_id,
        "snapshot_id": application.current_snapshot_id, **payload}).encode()).hexdigest()
    parent_id = str(uuid5(NAMESPACE_URL, f"job-workflow:{application_id}:{body.idempotency_key}"))
    replay = runtime.agent_tasks.plan_root(parent_id, body.idempotency_key)
    if replay:
        if replay.input_spec.get("request_hash") != request_hash:
            raise TaskConflictError("Idempotency key was used for a different workflow request.")
        return _summary(replay.task_id, runtime)
    if application.version != body.expected_application_version:
        raise TaskConflictError("Application version changed; refresh before starting.")
    linked_runs = runtime.workspace.associated_runs(application_id)
    source = None
    for item in linked_runs:
        resolved = runtime.packs.resume_source(application_id, item["run_id"])
        if resolved is not None:
            source = (item["run_id"], resolved)
            break
    if source is None:
        raise HTTPException(422, detail="Analyze and attach a resume run before starting this workflow.")
    source_run_id, source_data = source
    source_hash = hashlib.sha256(canonical_json({"resume_text": source_data[0],
        "job_description": runtime.workspace.current_snapshot(application_id).cleaned_job_description}).encode()).hexdigest()
    profile = {"standard": MultiAgentBudget(max_tasks=100, max_depth=20,
                   max_children_per_task=100, max_parallel_tasks=3),
               "extended": MultiAgentBudget(max_tasks=100, max_depth=20,
                   max_children_per_task=100, max_parallel_tasks=3,
                   max_input_tokens=180_000, max_output_tokens=50_000)}[body.budget_profile]
    job = runtime.workspace.application_job(application_id)
    linked_evidence_ids = tuple(sorted({link.evidence_id for link in
        runtime.evidence.list_for_application(application_id)})) if runtime.evidence else ()
    if len(linked_evidence_ids) > 50:
        raise HTTPException(422, detail="This Application has too many linked evidence items for one execution.")
    plan = build_pack_plan(application_id=application_id,
        parent_session_id=parent_id, idempotency_key=body.idempotency_key,
        requested_artifacts=frozenset(body.requested_artifacts),
        application_questions=tuple(body.application_questions),
        job_snapshot_id=application.current_snapshot_id,
        source_run_id=source_run_id, source_hash=source_hash,
        match_evidence_ids=linked_evidence_ids,
        include_interview=body.include_interview, company=job.company,
        title=job.title, budget=profile, request_hash=request_hash)
    validator = AgentPlanValidator(worker_types=runtime.agent_workers.names(),
        registered_tools=runtime.registry.names(), parent_tools=frozenset(),
        session_tools=frozenset(), task_type_tools=runtime.agent_scheduler.contexts.task_type_tools)
    depths = validator.validate(plan)
    if runtime.sessions.get(parent_id) is None:
        try:
            runtime.sessions.create(SessionState(session_id=parent_id, title="Job workflow supervisor",
                user_id="local-user", allowed_tools=frozenset()),
                SessionEvent(session_id=parent_id, event_type=SessionEventType.SESSION_CREATED))
        except SessionAlreadyExistsError:
            pass
    root = runtime.agent_tasks.create_plan(plan, depths)
    return _summary(root.task_id, runtime)


@router.get("/multi-agent-runs/{root_task_id}")
def get_multi_run(root_task_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    return _summary(root_task_id, runtime)


@router.get("/multi-agent-runs/{root_task_id}/tasks")
def get_multi_tasks(root_task_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    root = _require_root(root_task_id, runtime)
    return [_public(task, runtime) for task in [root, *runtime.agent_tasks.children(root_task_id)]]


@router.get("/multi-agent-runs/{root_task_id}/timeline")
def get_multi_timeline(root_task_id: str, runtime: SessionRuntime = Depends(get_session_runtime)):
    root = _require_root(root_task_id, runtime)
    tasks = [root, *runtime.agent_tasks.children(root_task_id)]
    events = [{"task_id": task.task_id, "sequence": event.sequence,
               "event_type": event.event_type.value,
               "occurred_at": event.occurred_at.isoformat()}
              for task in tasks for event in runtime.agent_tasks.events(task.task_id)]
    return sorted(events, key=lambda event: (event["occurred_at"], event["task_id"], event["sequence"]))


@router.post("/multi-agent-runs/{root_task_id}/cancel")
def cancel_multi_run(root_task_id: str, body: MultiRunMutation,
                     runtime: SessionRuntime = Depends(get_session_runtime)):
    _require_root(root_task_id, runtime)
    runtime.agent_tasks.cancel(root_task_id, expected_version=body.expected_version, subtree=True)
    return _summary(root_task_id, runtime)


@router.post("/multi-agent-runs/{root_task_id}/resume")
def resume_multi_run(root_task_id: str, body: MultiRunResume,
                     runtime: SessionRuntime = Depends(get_session_runtime)):
    _require_root(root_task_id, runtime)
    task = runtime.agent_tasks.require(body.task_id)
    if task.root_task_id != root_task_id:
        raise TaskNotFoundError("Task does not belong to this execution.")
    if body.continue_without_clarification:
        interview_id = (task.result_summary or {}).get("pause_metadata", {}).get("interview_id")
        if (task.task_type != "evidence_interview" or not interview_id or
                runtime.interviewer is None or
                runtime.interviewer.view(interview_id)["interview"]["status"] != "cancelled"):
            raise TaskConflictError("Cancel the evidence interview before continuing without it.")
    runtime.agent_tasks.resume(task.task_id, expected_version=body.expected_version,
        continue_without_clarification=body.continue_without_clarification)
    return _summary(root_task_id, runtime)
