"""Server-owned, deterministic analysis DAG for an opt-in Application run."""
from __future__ import annotations

from agent_runtime.multi_agent.types import (
    AgentPlan, MultiAgentBudget, TaskDependencySpec, TaskSpec,
)
from agent_runtime.application_pack.policy import classify_question, requires_manual_answer

TEMPLATE_ID = "job_application_multi_agent_v1"
# Child context snapshots include serialized source artifacts and metadata. A
# real JD can exceed the generic task default of 4,000 estimated tokens before
# its analysis worker starts. Keep a bounded, explicit budget for this workflow.
JOB_WORKFLOW_CONTEXT_TOKEN_BUDGET = 24_000


def build_analysis_plan(*, application_id: str, parent_session_id: str,
                        idempotency_key: str,
                        budget: MultiAgentBudget | None = None) -> AgentPlan:
    """Candidate and Job analyses share only a trusted source-provision parent.

    This bounded base graph deliberately stops after evidence-gap classification;
    generation is not scheduled until a frozen evidence snapshot exists.
    """
    common = {"application_id": application_id, "allowed_tools": frozenset(),
              "recent_parent_message_limit": 0,
              "token_budget": JOB_WORKFLOW_CONTEXT_TOKEN_BUDGET}
    tasks = [
        TaskSpec(key="source", task_type="job_source_provision", agent_role="Source preparation",
                 output_spec={"roles": ["resume_source", "job_source"]}, **common),
        TaskSpec(key="candidate", task_type="candidate_analysis", agent_role="Candidate analysis",
                 parent_key="source", input_from_tasks={"resume_source": "source"},
                 output_spec={"roles": ["candidate_profile"]}, **common),
        TaskSpec(key="job", task_type="job_analysis", agent_role="Job analysis",
                 parent_key="source", input_from_tasks={"job_source": "source"},
                 output_spec={"roles": ["job_analysis"]}, **common),
        TaskSpec(key="match", task_type="requirement_match", agent_role="Requirement matching",
                 parent_key="source", input_from_tasks={"candidate_profile": "candidate",
                                                        "job_analysis": "job"},
                 output_spec={"roles": ["match_report"]}, **common),
        TaskSpec(key="gap", task_type="evidence_gap", agent_role="Evidence gaps",
                 parent_key="source", input_from_tasks={"match_report": "match"},
                 output_spec={"roles": ["evidence_gap_plan"]}, **common),
    ]
    dependencies = [TaskDependencySpec(task_key=child, depends_on_key=parent)
                    for child, parent in (("candidate", "source"), ("job", "source"),
                                          ("match", "candidate"), ("match", "job"),
                                          ("gap", "match"))]
    return AgentPlan(template_id=TEMPLATE_ID, workflow_mode="multi_agent_v1",
        parent_session_id=parent_session_id, root_key="source", tasks=tasks,
        dependencies=dependencies, budget=budget or MultiAgentBudget(),
        idempotency_key=idempotency_key)


def build_pack_plan(*, application_id: str, parent_session_id: str,
                    idempotency_key: str, requested_artifacts: frozenset[str],
                    application_questions: tuple[str, ...] = (),
                    job_snapshot_id: str,
                    source_run_id: str | None = None,
                    source_hash: str | None = None,
                    match_evidence_ids: tuple[str, ...] = (),
                    include_interview: bool = False,
                    company: str | None = None, title: str | None = None,
                    max_revisions: int = 3,
                    request_hash: str | None = None,
                    budget: MultiAgentBudget | None = None) -> AgentPlan:
    """Expand only approved artifact types and bounded revision steps in Python."""
    if not requested_artifacts or requested_artifacts - {
        "tailored_resume", "cover_letter", "application_answer"}:
        raise ValueError("Requested artifact types are invalid.")
    if len(application_questions) > 3 or any(not q.strip() or len(q) > 5000 for q in application_questions):
        raise ValueError("At most three nonempty application questions are allowed.")
    if ("application_answer" in requested_artifacts) != bool(application_questions):
        raise ValueError("Application answers require explicit questions.")
    if not 0 <= max_revisions <= 3:
        raise ValueError("Revision limit must be at most three.")
    budget = budget or MultiAgentBudget(max_tasks=100, max_depth=20,
        max_children_per_task=100, max_parallel_tasks=3)
    base = build_analysis_plan(application_id=application_id,
        parent_session_id=parent_session_id, idempotency_key=idempotency_key,
        budget=budget)
    base.tasks[0].input_spec.update({"job_snapshot_id": job_snapshot_id,
        "source_run_id": source_run_id, "source_hash": source_hash})
    if len(match_evidence_ids) > 50 or len(set(match_evidence_ids)) != len(match_evidence_ids):
        raise ValueError("Scoped matching evidence is invalid.")
    for key in ("match", "gap"):
        next(task for task in base.tasks if task.key == key).input_spec["evidence_ids"] = list(match_evidence_ids)
    if request_hash:
        base.tasks[0].input_spec["request_hash"] = request_hash
    common = {"application_id": application_id, "parent_key": "source",
              "allowed_tools": frozenset(), "recent_parent_message_limit": 0,
              "token_budget": JOB_WORKFLOW_CONTEXT_TOKEN_BUDGET}
    base.tasks.append(TaskSpec(key="project", task_type="analysis_projection",
        agent_role="Analysis projection", input_spec={"job_snapshot_id": job_snapshot_id},
        input_from_tasks={"job_analysis": "job", "match_report": "match"},
        output_spec={"roles": ["projected_analysis"]}, **common))
    for parent in ("job", "match"):
        base.dependencies.append(TaskDependencySpec(task_key="project", depends_on_key=parent))
    base.dependencies.append(TaskDependencySpec(task_key="gap", depends_on_key="project"))
    freeze_inputs = {"job_analysis": "job", "evidence_gap_plan": "gap"}
    if include_interview:
        base.tasks.append(TaskSpec(key="interview", task_type="evidence_interview",
            agent_role="Evidence interview", max_attempts=25,
            input_from_tasks={"evidence_gap_plan": "gap"},
            output_spec={"roles": ["evidence_discovery_summary"]}, **common))
        base.dependencies.append(TaskDependencySpec(task_key="interview", depends_on_key="gap"))
        freeze_inputs["evidence_discovery_summary"] = "interview"
    base.tasks.append(TaskSpec(key="freeze", task_type="freeze_evidence",
        agent_role="Evidence freeze", input_spec={"job_snapshot_id": job_snapshot_id},
        input_from_tasks=freeze_inputs,
        output_spec={"roles": ["generation_evidence_snapshot"]}, **common))
    for parent in ("job", "gap", "project", *(("interview",) if include_interview else ())):
        base.dependencies.append(TaskDependencySpec(task_key="freeze", depends_on_key=parent))
    artifacts = ([('resume', 'tailored_resume', None)] if "tailored_resume" in requested_artifacts else [])
    if "cover_letter" in requested_artifacts:
        artifacts.append(("cover", "cover_letter", None))
    if "application_answer" in requested_artifacts:
        artifacts.extend((f"answer_{i}", "application_answer", question)
                         for i, question in enumerate(application_questions))
    final_keys = []
    for key, kind, question in artifacts:
        if question is not None and requires_manual_answer(classify_question(question)):
            # Restricted questions remain a manual Pack item; no writer task.
            continue
        writer_key, draft_role = f"{key}_writer", f"{key}_v0"
        settings = {"artifact_type": kind, "artifact_key": draft_role,
                    "question": question, "company": company, "title": title}
        inputs = {"generation_evidence_snapshot": "freeze",
                  "job_analysis": "job", "match_report": "match"}
        base.tasks.append(TaskSpec(key=writer_key, task_type="artifact_writer",
            agent_role=f"{kind} writing", input_spec=settings,
            input_from_tasks=inputs, output_spec={"roles": [draft_role]}, **common))
        for parent in ("freeze", "job", "match"):
            base.dependencies.append(TaskDependencySpec(task_key=writer_key, depends_on_key=parent))
        previous_key, previous_role = writer_key, draft_role
        previous_verify_key = None
        previous_report_role = None
        for revision in range(max_revisions + 1):
            verify_key, report_role = f"{key}_verify_{revision}", f"{key}_check_{revision}"
            verify_inputs = {**inputs, previous_role: previous_key}
            if previous_verify_key is not None:
                verify_inputs[previous_report_role] = previous_verify_key
            base.tasks.append(TaskSpec(key=verify_key, task_type="artifact_verifier",
                agent_role=f"{kind} verification", input_spec={
                    "artifact_type": kind, "artifact_key": previous_role,
                    "report_key": report_role, "question": question,
                    "prior_report_key": previous_report_role},
                input_from_tasks=verify_inputs,
                output_spec={"roles": [report_role]}, **common))
            for parent in ("freeze", "job", "match", previous_key,
                           *((previous_verify_key,) if previous_verify_key else ())):
                base.dependencies.append(TaskDependencySpec(task_key=verify_key, depends_on_key=parent))
            if revision == max_revisions:
                final_keys.append((key, kind, question, previous_key, previous_role,
                                   verify_key, report_role))
                break
            revised_key, revised_role = f"{key}_revise_{revision + 1}", f"{key}_v{revision + 1}"
            base.tasks.append(TaskSpec(key=revised_key, task_type="artifact_revision",
                agent_role=f"{kind} revision", input_spec={
                    "artifact_type": kind, "artifact_key": previous_role,
                    "report_key": report_role, "revised_key": revised_role,
                    "question": question, "company": company, "title": title},
                input_from_tasks={**inputs, previous_role: previous_key,
                    report_role: verify_key}, output_spec={"roles": [revised_role]}, **common))
            for parent in ("freeze", "job", "match", previous_key, verify_key):
                base.dependencies.append(TaskDependencySpec(task_key=revised_key, depends_on_key=parent))
            previous_verify_key, previous_report_role = verify_key, report_role
            previous_key, previous_role = revised_key, revised_role
    if not final_keys:
        raise ValueError("At least one generatable artifact is required.")
    mappings = {"generation_evidence_snapshot": "freeze"}
    optional_mappings = {}
    final_specs = []
    has_resume = any(kind == "tailored_resume" for _, kind, *_ in final_keys)
    for index, (key, kind, question, artifact_task, artifact_role, verify_task, report_role) in enumerate(final_keys):
        required = kind == "tailored_resume" or (not has_resume and index == 0)
        target = mappings if required else optional_mappings
        target[artifact_role] = artifact_task
        target[report_role] = verify_task
        final_specs.append({"key": key, "artifact_type": kind, "question": question,
                            "artifact_role": artifact_role, "report_role": report_role,
                            "required": required})
    base.tasks.append(TaskSpec(key="assemble", task_type="pack_assembler",
        agent_role="Pack assembly", input_spec={"final_artifacts": final_specs,
            "manual_questions": [q for q in application_questions
                if requires_manual_answer(classify_question(q))]},
        input_from_tasks=mappings, optional_input_from_tasks=optional_mappings,
        output_spec={"roles": ["application_pack_summary"]},
        **common))
    for source in set(mappings.values()):
        base.dependencies.append(TaskDependencySpec(task_key="assemble", depends_on_key=source))
    for source in set(optional_mappings.values()):
        base.dependencies.append(TaskDependencySpec(task_key="assemble", depends_on_key=source,
            dependency_type="requires_completion"))
    return AgentPlan.model_validate(base.model_dump(mode="python"))
