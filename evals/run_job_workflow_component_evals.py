"""No-model component gate for the opt-in Job workflow, not a live parity claim."""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from evals.run_application_pack_evals import run as run_pack_safety
from agent_runtime.job_workflow.plan import build_pack_plan
from agent_runtime.application_pack.workflow import PACK_PROMPT_VERSION
from job_agent.prompts import PROMPT_VERSION


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "results" / "multi_agent_job_workflow_v0.1_component.json"


def run() -> dict:
    safety = run_pack_safety()
    plan = build_pack_plan(application_id="synthetic-application",
        parent_session_id="synthetic-parent", idempotency_key="component-eval",
        job_snapshot_id="synthetic-snapshot",
        requested_artifacts=frozenset({"tailored_resume", "cover_letter", "application_answer"}),
        application_questions=("Describe your Python experience.",),
        include_interview=True, max_revisions=3)
    keys = {task.key: task for task in plan.tasks}
    required_edges = {("candidate", "source"), ("job", "source"),
        ("match", "candidate"), ("match", "job"), ("freeze", "interview")}
    actual_edges = {(edge.task_key, edge.depends_on_key) for edge in plan.dependencies}
    git_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT.parent,
        capture_output=True, text=True, check=False).stdout.strip() or None
    dirty_before_run = bool(subprocess.run(["git", "status", "--porcelain"],
        cwd=ROOT.parent, capture_output=True, text=True, check=False).stdout.strip())
    shared = {"cases": safety["metrics"]["cases"],
        "expected_behavior_achieved": safety["metrics"]["expected_behavior_achieved"],
        "unsupported_claim_detection_recall": safety["metrics"]["unsupported_claim_detection_recall"],
        "supported_claim_false_positive_rate": safety["metrics"]["supported_claim_false_positive_rate"],
        "restricted_question_refusal_accuracy": safety["metrics"]["restricted_question_refusal_accuracy"]}
    return {"run_metadata": {"timestamp": datetime.now(UTC).isoformat(),
        "git_commit": git_commit, "dirty_before_run": dirty_before_run,
        "dataset_version": "application-pack-v1",
        "prompt_version": PROMPT_VERSION,
        "pack_prompt_version": PACK_PROMPT_VERSION,
        "evaluator": "shared-deterministic-component-gate", "model_calls": 0,
        "runs_per_case": 1},
        "single_custom_shared_safety": shared,
        "multi_agent_v1_shared_safety": shared,
        "plan_checks": {"task_count": len(plan.tasks),
            "required_dependencies_present": required_edges <= actual_edges,
            "candidate_and_job_are_siblings": keys["candidate"].parent_key == keys["job"].parent_key,
            "all_worker_tool_allowlists_empty": all(not task.allowed_tools for task in plan.tasks),
            "interview_is_explicit": "interview" in keys,
            "max_answer_tasks": 3, "max_revisions_per_artifact": 3},
        "not_measured": ["full_workflow_parity", "parallel_speedup", "live_quality",
            "latency", "token_usage", "estimated_cost", "model_nondeterminism",
            "recovery_after_live_provider_failure"],
        "case_results": safety["results"]}


if __name__ == "__main__":
    result = run()
    OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{result['single_custom_shared_safety']['expected_behavior_achieved']}/"
          f"{result['single_custom_shared_safety']['cases']} shared safety cases; {OUTPUT}")
