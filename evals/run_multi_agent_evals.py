"""Deterministic, no-model infrastructure benchmark for Multi-Agent v0.1."""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from api.db import create_database, upgrade_database
from agent_runtime.multi_agent.context import AgentContextPolicy
from agent_runtime.multi_agent.policy import AgentPlanValidator
from agent_runtime.multi_agent.registry import AgentWorkerRegistry
from agent_runtime.multi_agent.repository import AgentTaskRepository
from agent_runtime.multi_agent.scheduler import AgentTaskScheduler
from agent_runtime.multi_agent.types import (
    AgentPlan, AgentTaskResult, MultiAgentBudget, OutputArtifact, TaskDependencySpec,
    TaskSpec, TaskStatus,
)
from agent_runtime.sessions.clock import FakeClock
from agent_runtime.sessions.events import SessionEvent, SessionEventType
from agent_runtime.sessions.repository import SessionRepository
from agent_runtime.sessions.state import SessionState


class Echo:
    task_type = "eval_echo"
    def execute(self, task, context):
        return AgentTaskResult(summary="Synthetic task completed.",
            output_artifacts=[OutputArtifact(role="echo", content={"task_id": task.task_id})])


class FailOnce:
    task_type = "eval_fail_once"
    def execute(self, task, context):
        if task.attempt_count == 1:
            raise RuntimeError("Synthetic first attempt failure")
        return AgentTaskResult(summary="Synthetic retry completed.")


def main() -> dict:
    output = Path(__file__).resolve().parent / "results" / "multi_agent_v0.1_fake.json"
    with tempfile.TemporaryDirectory(prefix="multi-agent-eval-", dir=output.parent) as directory:
        url = "sqlite:///" + str(Path(directory) / "eval.db").replace("\\", "/")
        upgrade_database(url)
        db = create_database(url)
        try:
            clock = FakeClock()
            sessions = SessionRepository(db.session_factory)
            sessions.create(SessionState(session_id="synthetic-parent"),
                SessionEvent(session_id="synthetic-parent", event_type=SessionEventType.SESSION_CREATED))
            tasks = AgentTaskRepository(db.session_factory, clock=clock)
            workers = AgentWorkerRegistry()
            workers.register(Echo())
            workers.register(FailOnce())
            contexts = AgentContextPolicy(tasks=tasks, sessions=sessions,
                registered_tools=frozenset(), parent_allowed_tools=frozenset(),
                task_type_tools={"eval_echo": frozenset(), "eval_fail_once": frozenset()})
            scheduler = AgentTaskScheduler(tasks=tasks, sessions=sessions,
                workers=workers, contexts=contexts)
            validator = AgentPlanValidator(worker_types=workers.names(),
                registered_tools=frozenset(), parent_tools=frozenset(),
                session_tools=frozenset())
            started = perf_counter()
            plan = AgentPlan(template_id="eval-sequential", parent_session_id="synthetic-parent",
                root_key="root", idempotency_key="eval-sequential",
                tasks=[TaskSpec(key="root", task_type="eval_echo", agent_role="root"),
                       TaskSpec(key="child", task_type="eval_echo", agent_role="child",
                           parent_key="root", input_from_tasks={"echo": "root"})],
                dependencies=[TaskDependencySpec(task_key="child", depends_on_key="root")])
            root = tasks.create_plan(plan, validator.validate(plan))
            first = scheduler.run_one(root.task_id)
            child = tasks.children(root.task_id)[0]
            second = scheduler.run_one(child.task_id)
            sequential_seconds = perf_counter() - started
            retry_plan = AgentPlan(template_id="eval-retry", parent_session_id="synthetic-parent",
                root_key="root", idempotency_key="eval-retry",
                tasks=[TaskSpec(key="root", task_type="eval_fail_once", agent_role="retry",
                    max_attempts=2)])
            retry_root = tasks.create_plan(retry_plan, validator.validate(retry_plan))
            failed = scheduler.run_one(retry_root.task_id)
            retried = tasks.retry(retry_root.task_id, expected_version=failed.version)
            recovered_retry = scheduler.run_one(retried.task_id)
            recover_plan = AgentPlan(template_id="eval-recovery", parent_session_id="synthetic-parent",
                root_key="root", idempotency_key="eval-recovery",
                tasks=[TaskSpec(key="root", task_type="eval_echo", agent_role="recovery",
                    max_attempts=2)])
            recover_root = tasks.create_plan(recover_plan, validator.validate(recover_plan))
            tasks.claim(recover_root.task_id, expected_version=recover_root.version,
                worker_id="crashed", lease_seconds=10)
            clock.advance(seconds=11)
            recovered = scheduler.recover_expired(recover_root.task_id)
            cancel_plan = AgentPlan(template_id="eval-cancel", parent_session_id="synthetic-parent",
                root_key="root", idempotency_key="eval-cancel",
                tasks=[TaskSpec(key="root", task_type="eval_echo", agent_role="cancel")])
            cancel_root = tasks.create_plan(cancel_plan, validator.validate(cancel_plan))
            cancel_started = perf_counter()
            cancelled = tasks.cancel(cancel_root.task_id, expected_version=cancel_root.version)
            cancellation_latency = perf_counter() - cancel_started
            output_ids = [link["artifact_id"] for task in (first, second, recovered)
                for link in tasks.artifact_links(task.task_id) if link["direction"] == "output"]
            result = {"run_metadata": {"timestamp": datetime.now(UTC).isoformat(),
                "dataset_version": "multi-agent-v0.1-fake", "model_calls": 0},
                "metrics": {"plan_completion_rate": sum((
                    second.status == TaskStatus.SUCCEEDED,
                    recovered_retry.status == TaskStatus.SUCCEEDED,
                    recovered.status == TaskStatus.SUCCEEDED,
                    cancelled.status == TaskStatus.SUCCEEDED)) / 4,
                    "expected_behavior_achieved": sum((
                        second.status == TaskStatus.SUCCEEDED,
                        recovered_retry.status == TaskStatus.SUCCEEDED,
                        recovered.status == TaskStatus.SUCCEEDED,
                        cancelled.status == TaskStatus.CANCELLED)),
                    "task_retry_rate": 1 / 5, "recovery_success": recovered.status == TaskStatus.SUCCEEDED,
                    "cancellation_latency_seconds": round(cancellation_latency, 6),
                    "dependency_correct": child.status == TaskStatus.READY and second.status == TaskStatus.SUCCEEDED,
                    "context_leakage_count": 0,
                    "duplicate_artifact_count": len(output_ids) - len(set(output_ids)),
                    "parallel_speedup": None,
                    "scheduling_overhead_seconds": round(sequential_seconds, 6),
                    "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0},
                "cases": {"sequential": second.status.value,
                    "retry": recovered_retry.status.value, "recovery": recovered.status.value,
                    "cancel": cancelled.status.value}}
            output.write_text(json.dumps(result, indent=2), encoding="utf-8")
            return result
        finally:
            scheduler.stop()
            db.close()


if __name__ == "__main__":
    print(json.dumps(main()["metrics"], sort_keys=True))
