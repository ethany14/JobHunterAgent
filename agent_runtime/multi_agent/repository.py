"""Short transactional task persistence with version- and attempt-fenced writes."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agent_runtime.context.models import ContextSnapshotRow
from agent_runtime.multi_agent.errors import BudgetExceededError, LeaseConflictError, TaskConflictError, TaskNotFoundError
from agent_runtime.multi_agent.models import (
    AgentPlanRow, AgentTaskArtifactLinkRow, AgentTaskArtifactRow, AgentTaskAttemptRow,
    AgentTaskDependencyRow, AgentTaskEventRow, AgentTaskRow,
)
from agent_runtime.multi_agent.policy import dependency_ready
from agent_runtime.multi_agent.types import (
    AgentPlan, AgentTask, AgentTaskEvent, AgentTaskResult, ArtifactDirection,
    DependencyType, TaskEventType, TaskStatus, TERMINAL,
)
from agent_runtime.security import canonical_json
from agent_runtime.sessions.clock import Clock, SystemClock
from agent_runtime.workspace.models import ApplicationArtifactRow
from agent_runtime.models import ToolCallEventRow, ToolCallRow


def _utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class AgentTaskRepository:
    def __init__(self, session_factory: sessionmaker[Session], *, clock: Clock | None = None) -> None:
        self.sessions = session_factory
        self.clock = clock or SystemClock()

    @staticmethod
    def _task(row: AgentTaskRow) -> AgentTask:
        return AgentTask(task_id=row.task_id, task_type=row.task_type, agent_role=row.agent_role,
            parent_task_id=row.parent_task_id, root_task_id=row.root_task_id,
            parent_session_id=row.parent_session_id, child_session_id=row.child_session_id,
            application_id=row.application_id, status=row.status, priority=row.priority,
            depth=row.depth, version=row.version, event_sequence=row.event_sequence,
            attempt_count=row.attempt_count, max_attempts=row.max_attempts,
            active_attempt_id=row.active_attempt_id, lease_until=_utc(row.lease_until),
            deadline_at=_utc(row.deadline_at), idempotency_key=row.idempotency_key,
            input_spec=json.loads(row.input_spec_json), output_spec=json.loads(row.output_spec_json),
            result_summary=json.loads(row.result_summary_json) if row.result_summary_json else None,
            error_code=row.error_code, created_at=_utc(row.created_at), updated_at=_utc(row.updated_at),
            started_at=_utc(row.started_at), completed_at=_utc(row.completed_at))

    def _event(self, db: Session, row: AgentTaskRow, event_type: TaskEventType,
               *, attempt_id: str | None = None, payload: dict | None = None) -> None:
        row.event_sequence += 1
        db.add(AgentTaskEventRow(event_id=str(uuid4()), task_id=row.task_id,
            sequence=row.event_sequence, event_type=event_type.value,
            attempt_id=attempt_id, payload_json=canonical_json(payload or {}),
            occurred_at=self.clock.now()))

    def create_plan(self, plan: AgentPlan, depths: dict[str, int]) -> AgentTask:
        try:
            return self._create_plan_once(plan, depths)
        except IntegrityError as exc:
            digest = hashlib.sha256(canonical_json(plan.model_dump(mode="json", exclude={"idempotency_key"})).encode()).hexdigest()
            with self.sessions() as db:
                existing = db.scalar(select(AgentPlanRow).where(
                    AgentPlanRow.parent_session_id == plan.parent_session_id,
                    AgentPlanRow.idempotency_key == plan.idempotency_key))
                if existing is None:
                    raise
                if existing.plan_hash != digest:
                    raise TaskConflictError("Plan idempotency key was reused with different input.") from exc
                return self._task(db.get(AgentTaskRow, existing.root_task_id))

    def _create_plan_once(self, plan: AgentPlan, depths: dict[str, int]) -> AgentTask:
        """Validate first; insert the entire DAG and initial audit in one transaction."""
        digest = hashlib.sha256(canonical_json(plan.model_dump(mode="json", exclude={"idempotency_key"})).encode()).hexdigest()
        now = self.clock.now()
        with self.sessions.begin() as db:
            existing = db.scalar(select(AgentPlanRow).where(
                AgentPlanRow.parent_session_id == plan.parent_session_id,
                AgentPlanRow.idempotency_key == plan.idempotency_key))
            if existing:
                if existing.plan_hash != digest:
                    raise TaskConflictError("Plan idempotency key was reused with different input.")
                return self._task(db.get(AgentTaskRow, existing.root_task_id))
            ids = {spec.key: str(uuid4()) for spec in plan.tasks}
            root_id = ids[plan.root_key]
            db.add(AgentPlanRow(plan_id=str(uuid4()), parent_session_id=plan.parent_session_id,
                workflow_mode=plan.workflow_mode,
                idempotency_key=plan.idempotency_key, plan_hash=digest, root_task_id=root_id,
                budget_json=canonical_json(plan.budget.model_dump(mode="json")), created_at=now))
            for spec in plan.tasks:
                task_deps = [dep for dep in plan.dependencies if dep.task_key == spec.key]
                status = TaskStatus.BLOCKED if task_deps else TaskStatus.READY
                row = AgentTaskRow(task_id=ids[spec.key], task_type=spec.task_type,
                    agent_role=spec.agent_role, parent_task_id=ids.get(spec.parent_key),
                    root_task_id=root_id, parent_session_id=plan.parent_session_id,
                    application_id=spec.application_id, status=status.value,
                    priority=spec.priority, depth=depths[spec.key], version=0,
                    event_sequence=0, attempt_count=0, max_attempts=spec.max_attempts,
                    deadline_at=min(filter(None, (spec.deadline_at, plan.budget.deadline_at)), default=None),
                    idempotency_key=spec.key, input_spec_json=canonical_json({
                        **spec.input_spec, "allowed_tools": sorted(spec.allowed_tools),
                        "allowed_skill_version_ids": sorted(spec.allowed_skill_version_ids),
                        "input_artifact_ids": spec.input_artifact_ids,
                        "input_from_tasks": spec.input_from_tasks,
                        "optional_input_from_tasks": spec.optional_input_from_tasks,
                        "recent_parent_message_limit": spec.recent_parent_message_limit,
                        "token_budget": spec.token_budget}),
                    output_spec_json=canonical_json(spec.output_spec),
                    created_at=now, updated_at=now)
                db.add(row)
            db.flush()
            for spec in plan.tasks:
                row = db.get(AgentTaskRow, ids[spec.key])
                self._event(db, row, TaskEventType.TASK_CREATED)
                if row.status == TaskStatus.READY.value:
                    self._event(db, row, TaskEventType.TASK_READY)
            db.flush()
            for dep in plan.dependencies:
                db.add(AgentTaskDependencyRow(task_id=ids[dep.task_key],
                    depends_on_task_id=ids[dep.depends_on_key],
                    dependency_type=dep.dependency_type.value, created_at=now))
                self._event(db, db.get(AgentTaskRow, ids[dep.task_key]), TaskEventType.DEPENDENCY_ADDED,
                    payload={"depends_on_task_id": ids[dep.depends_on_key], "type": dep.dependency_type.value})
            for spec in plan.tasks:
                for artifact_id in spec.input_artifact_ids:
                    db.add(AgentTaskArtifactLinkRow(task_id=ids[spec.key], artifact_id=artifact_id,
                        direction=ArtifactDirection.INPUT.value, role="input", created_at=now))
            db.flush()
            return self._task(db.get(AgentTaskRow, root_id))

    def get(self, task_id: str) -> AgentTask | None:
        with self.sessions() as db:
            row = db.get(AgentTaskRow, task_id)
            return self._task(row) if row else None

    def require(self, task_id: str) -> AgentTask:
        task = self.get(task_id)
        if task is None:
            raise TaskNotFoundError("Task does not exist.")
        return task

    def children(self, task_id: str) -> list[AgentTask]:
        with self.sessions() as db:
            return [self._task(r) for r in db.scalars(select(AgentTaskRow).where(
                AgentTaskRow.parent_task_id == task_id).order_by(AgentTaskRow.created_at, AgentTaskRow.task_id))]

    def for_application(self, application_id: str) -> list[AgentTask]:
        with self.sessions() as db:
            return [self._task(r) for r in db.scalars(select(AgentTaskRow).where(
                AgentTaskRow.application_id == application_id).order_by(
                AgentTaskRow.created_at.desc(), AgentTaskRow.task_id).limit(100))]

    def dependencies(self, task_id: str) -> list[dict[str, str]]:
        self.require(task_id)
        with self.sessions() as db:
            return [{"depends_on_task_id": row.depends_on_task_id,
                "dependency_type": row.dependency_type} for row in db.scalars(
                select(AgentTaskDependencyRow).where(AgentTaskDependencyRow.task_id == task_id)
                .order_by(AgentTaskDependencyRow.depends_on_task_id))]

    def list_ready(self, limit: int = 20) -> list[AgentTask]:
        with self.sessions() as db:
            return [self._task(r) for r in db.scalars(select(AgentTaskRow).where(
                AgentTaskRow.status == TaskStatus.READY.value)
                .order_by(AgentTaskRow.priority.desc(), AgentTaskRow.created_at, AgentTaskRow.task_id).limit(limit))]

    def list_expired(self, limit: int = 20) -> list[AgentTask]:
        now = self.clock.now()
        with self.sessions() as db:
            return [self._task(r) for r in db.scalars(select(AgentTaskRow).where(
                AgentTaskRow.status.in_([TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value,
                    TaskStatus.CANCEL_REQUESTED.value]),
                AgentTaskRow.lease_until <= now)
                .order_by(AgentTaskRow.lease_until, AgentTaskRow.task_id).limit(limit))]

    def list_due(self, limit: int = 20) -> list[AgentTask]:
        now = self.clock.now()
        with self.sessions() as db:
            return [self._task(r) for r in db.scalars(select(AgentTaskRow).where(
                AgentTaskRow.deadline_at <= now,
                AgentTaskRow.status.not_in([status.value for status in TERMINAL]))
                .order_by(AgentTaskRow.deadline_at, AgentTaskRow.task_id).limit(limit))]

    def budget_for(self, root_task_id: str):
        from agent_runtime.multi_agent.types import MultiAgentBudget
        with self.sessions() as db:
            row = db.scalar(select(AgentPlanRow).where(AgentPlanRow.root_task_id == root_task_id))
            if row is None:
                raise TaskNotFoundError("Task plan does not exist.")
            return MultiAgentBudget.model_validate_json(row.budget_json)

    def plan_mode(self, root_task_id: str) -> str:
        with self.sessions() as db:
            row = db.scalar(select(AgentPlanRow).where(AgentPlanRow.root_task_id == root_task_id))
            if row is None:
                raise TaskNotFoundError("Task plan does not exist.")
            return row.workflow_mode

    def plan_root(self, parent_session_id: str, idempotency_key: str) -> AgentTask | None:
        with self.sessions() as db:
            plan = db.scalar(select(AgentPlanRow).where(
                AgentPlanRow.parent_session_id == parent_session_id,
                AgentPlanRow.idempotency_key == idempotency_key))
            return self._task(db.get(AgentTaskRow, plan.root_task_id)) if plan else None

    def root_activity(self, root_task_id: str) -> tuple[int, int]:
        with self.sessions() as db:
            rows = db.scalars(select(AgentTaskRow).where(AgentTaskRow.root_task_id == root_task_id)).all()
            active = sum(r.status in {TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value} for r in rows)
            return len(rows), active

    def claim(self, task_id: str, *, expected_version: int, worker_id: str,
              lease_seconds: float = 60) -> AgentTask:
        now = self.clock.now()
        lease = now + timedelta(seconds=lease_seconds)
        attempt_id = str(uuid4())
        with self.sessions.begin() as db:
            row = db.get(AgentTaskRow, task_id)
            if row is None:
                raise TaskNotFoundError("Task does not exist.")
            if row.deadline_at and _utc(row.deadline_at) <= now:
                raise TaskConflictError("Task deadline has passed.")
            if row.status in {TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value} and (
                row.lease_until is None or _utc(row.lease_until) > now):
                raise LeaseConflictError("Task has an active claim.")
            if row.attempt_count >= row.max_attempts:
                raise BudgetExceededError("Task attempt limit reached.")
            prior = row.active_attempt_id
            changed = db.execute(update(AgentTaskRow).where(AgentTaskRow.task_id == task_id,
                AgentTaskRow.version == expected_version,
                or_(AgentTaskRow.status == TaskStatus.READY.value,
                    and_(AgentTaskRow.status.in_([TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value]),
                         AgentTaskRow.lease_until <= now)))
                .values(status=TaskStatus.CLAIMED.value, active_attempt_id=attempt_id,
                        lease_until=lease, attempt_count=AgentTaskRow.attempt_count + 1,
                        version=AgentTaskRow.version + 1, updated_at=now),
                execution_options={"synchronize_session": False}).rowcount
            if changed != 1:
                raise LeaseConflictError("Task has an active claim or stale version.")
            db.refresh(row)
            if prior:
                old = db.get(AgentTaskAttemptRow, prior)
                if old:
                    old.status, old.completed_at = "expired", now
                    if old.context_snapshot_id:
                        snapshot = db.get(ContextSnapshotRow, old.context_snapshot_id)
                        if snapshot and snapshot.status == "prepared":
                            snapshot.status, snapshot.abandoned_at = "abandoned", now
                            snapshot.version += 1
                self._event(db, row, TaskEventType.LEASE_EXPIRED, attempt_id=prior)
                self._event(db, row, TaskEventType.TASK_RECOVERED, attempt_id=attempt_id)
            db.add(AgentTaskAttemptRow(attempt_id=attempt_id, task_id=task_id,
                attempt_number=row.attempt_count, status="claimed", worker_id=worker_id,
                lease_until=lease, started_at=now))
            self._event(db, row, TaskEventType.TASK_CLAIMED, attempt_id=attempt_id)
            db.flush()
            return self._task(row)

    def heartbeat(self, task_id: str, attempt_id: str, *, lease_seconds: float) -> bool:
        now = self.clock.now()
        lease = now + timedelta(seconds=lease_seconds)
        with self.sessions.begin() as db:
            count = db.execute(update(AgentTaskRow).where(AgentTaskRow.task_id == task_id,
                AgentTaskRow.active_attempt_id == attempt_id,
                AgentTaskRow.status.in_([TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value]),
                AgentTaskRow.lease_until > now).values(lease_until=lease)).rowcount
            if count:
                db.execute(update(AgentTaskAttemptRow).where(
                    AgentTaskAttemptRow.attempt_id == attempt_id).values(lease_until=lease))
            return bool(count)

    def start(self, task_id: str, attempt_id: str, *, child_session_id: str,
              context_snapshot_id: str, context_hash: str, manifest: dict) -> AgentTask:
        with self.sessions.begin() as db:
            row = self._owned(db, task_id, attempt_id, {TaskStatus.CLAIMED})
            # Each attempt receives a fresh child session. Older attempt sessions
            # remain durable for audit but cannot own the task after the claim changes.
            row.child_session_id = child_session_id
            row.status = TaskStatus.RUNNING.value
            row.started_at = row.started_at or self.clock.now()
            row.version += 1
            row.updated_at = self.clock.now()
            attempt = db.get(AgentTaskAttemptRow, attempt_id)
            attempt.status = "running"
            attempt.context_snapshot_id = context_snapshot_id
            db.add(ContextSnapshotRow(snapshot_id=context_snapshot_id, session_id=child_session_id,
                status="prepared", context_hash=context_hash, manifest_json=canonical_json(manifest),
                version=0, prepared_at=self.clock.now()))
            self._event(db, row, TaskEventType.CHILD_SESSION_CREATED, attempt_id=attempt_id,
                payload={"child_session_id": child_session_id})
            self._event(db, row, TaskEventType.CONTEXT_ASSEMBLED, attempt_id=attempt_id,
                payload={"snapshot_id": context_snapshot_id, "context_hash": context_hash})
            self._event(db, row, TaskEventType.TASK_STARTED, attempt_id=attempt_id)
            return self._task(row)

    def complete(self, task_id: str, attempt_id: str, result: AgentTaskResult) -> AgentTask:
        now = self.clock.now()
        with self.sessions.begin() as db:
            row = self._owned(db, task_id, attempt_id, {TaskStatus.RUNNING})
            attempt = db.get(AgentTaskAttemptRow, attempt_id)
            budget_row = db.scalar(select(AgentPlanRow).where(AgentPlanRow.root_task_id == row.root_task_id))
            budget = json.loads(budget_row.budget_json)
            current_usage: dict[str, float] = {}
            for prior in db.scalars(select(AgentTaskRow).where(
                AgentTaskRow.root_task_id == row.root_task_id,
                AgentTaskRow.result_summary_json.is_not(None))):
                for key, value in json.loads(prior.result_summary_json).get("usage", {}).items():
                    current_usage[key] = current_usage.get(key, 0) + float(value)
            limit_keys = {"model_calls": "max_model_calls", "tool_calls": "max_tool_calls",
                "input_tokens": "max_input_tokens", "output_tokens": "max_output_tokens",
                "estimated_cost": "max_estimated_cost"}
            exceeded = False
            for key, limit_key in limit_keys.items():
                value = float(result.usage.get(key, 0))
                limit = budget.get(limit_key)
                if value < 0 or not math.isfinite(value) or (limit is not None and current_usage.get(key, 0) + value > limit):
                    exceeded = True
            if exceeded:
                row.status, row.active_attempt_id, row.lease_until = TaskStatus.FAILED.value, None, None
                row.error_code = "budget_exceeded"
                row.completed_at = row.updated_at = now
                row.version += 1
                attempt.status, attempt.completed_at, attempt.error_code = "failed", now, row.error_code
                snap = db.get(ContextSnapshotRow, attempt.context_snapshot_id)
                if snap:
                    snap.status, snap.abandoned_at, snap.version = "abandoned", now, snap.version + 1
                self._event(db, row, TaskEventType.TASK_FAILED, attempt_id=attempt_id,
                    payload={"error_code": row.error_code})
                self._unlock_dependents(db, task_id)
                return self._task(row)
            for artifact in result.output_artifacts:
                artifact_id = str(uuid4())
                content_json = canonical_json(artifact.content)
                if len(content_json.encode("utf-8")) > 50_000:
                    raise TaskConflictError("Task output artifact exceeds the size limit.")
                db.add(AgentTaskArtifactRow(artifact_id=artifact_id, task_id=task_id,
                    workflow_mode=budget_row.workflow_mode,
                    attempt_id=attempt_id, role=artifact.role, content_json=content_json,
                    content_hash=hashlib.sha256(content_json.encode()).hexdigest(), created_at=now))
                db.add(AgentTaskArtifactLinkRow(task_id=task_id, artifact_id=artifact_id,
                    direction=ArtifactDirection.OUTPUT.value, role=artifact.role, created_at=now))
                self._event(db, row, TaskEventType.ARTIFACT_CREATED, attempt_id=attempt_id,
                    payload={"artifact_id": artifact_id, "role": artifact.role})
            row.status, row.active_attempt_id, row.lease_until = TaskStatus.SUCCEEDED.value, None, None
            row.result_summary_json = canonical_json({"summary": result.summary,
                "usage": result.usage, "next_action_suggestions": result.next_action_suggestions})
            row.completed_at, row.updated_at = now, now
            row.version += 1
            attempt.status, attempt.completed_at = "succeeded", now
            attempt.usage_json = canonical_json(result.usage)
            snap = db.get(ContextSnapshotRow, attempt.context_snapshot_id)
            if snap:
                snap.status, snap.used_at, snap.version = "used", now, snap.version + 1
            self._event(db, row, TaskEventType.TASK_SUCCEEDED, attempt_id=attempt_id)
            self._unlock_dependents(db, task_id)
            db.flush()
            return self._task(row)

    def fail(self, task_id: str, attempt_id: str, *, error_code: str = "worker_failed") -> AgentTask:
        with self.sessions.begin() as db:
            row = self._owned(db, task_id, attempt_id, {TaskStatus.CLAIMED, TaskStatus.RUNNING})
            now = self.clock.now()
            row.status = TaskStatus.FAILED.value
            row.error_code = error_code[:64]
            row.active_attempt_id = None
            row.lease_until = None
            row.completed_at = row.updated_at = now
            row.version += 1
            attempt = db.get(AgentTaskAttemptRow, attempt_id)
            attempt.status, attempt.completed_at, attempt.error_code = "failed", now, error_code[:64]
            if attempt.context_snapshot_id:
                snap = db.get(ContextSnapshotRow, attempt.context_snapshot_id)
                if snap:
                    snap.status, snap.abandoned_at, snap.version = "abandoned", now, snap.version + 1
            self._event(db, row, TaskEventType.TASK_FAILED, attempt_id=attempt_id,
                payload={"error_code": error_code[:64]})
            self._unlock_dependents(db, task_id)
            return self._task(row)

    def timeout(self, task_id: str, attempt_id: str) -> AgentTask:
        with self.sessions.begin() as db:
            row = db.get(AgentTaskRow, task_id)
            if row is None or row.active_attempt_id != attempt_id or row.status not in {
                TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value}:
                raise LeaseConflictError("Timeout attempt is stale.")
            now = self.clock.now()
            row.status, row.active_attempt_id, row.lease_until = TaskStatus.TIMED_OUT.value, None, None
            row.error_code = "task_deadline_exceeded"
            row.version += 1
            row.updated_at = row.completed_at = now
            attempt = db.get(AgentTaskAttemptRow, attempt_id)
            attempt.status, attempt.completed_at, attempt.error_code = "timed_out", now, row.error_code
            if attempt.context_snapshot_id:
                snap = db.get(ContextSnapshotRow, attempt.context_snapshot_id)
                if snap:
                    snap.status, snap.abandoned_at, snap.version = "abandoned", now, snap.version + 1
            self._event(db, row, TaskEventType.TASK_TIMED_OUT, attempt_id=attempt_id)
            self._unlock_dependents(db, task_id)
            return self._task(row)

    def expire_due(self, task_id: str, *, expected_version: int) -> AgentTask:
        with self.sessions.begin() as db:
            row = db.get(AgentTaskRow, task_id)
            if row is None:
                raise TaskNotFoundError("Task does not exist.")
            now = self.clock.now()
            if row.version != expected_version or row.status in {s.value for s in TERMINAL} or (
                row.deadline_at is None or _utc(row.deadline_at) > now):
                raise TaskConflictError("Task deadline is not due or version changed.")
            self._deny_pending_approvals(db, task_id, now, "task_deadline_exceeded")
            attempt_id = row.active_attempt_id
            if attempt_id:
                attempt = db.get(AgentTaskAttemptRow, attempt_id)
                attempt.status, attempt.completed_at, attempt.error_code = "timed_out", now, "task_deadline_exceeded"
                if attempt.context_snapshot_id:
                    snap = db.get(ContextSnapshotRow, attempt.context_snapshot_id)
                    if snap and snap.status == "prepared":
                        snap.status, snap.abandoned_at, snap.version = "abandoned", now, snap.version + 1
            row.status, row.active_attempt_id, row.lease_until = TaskStatus.TIMED_OUT.value, None, None
            row.error_code = "task_deadline_exceeded"
            row.completed_at = row.updated_at = now
            row.version += 1
            self._event(db, row, TaskEventType.TASK_TIMED_OUT, attempt_id=attempt_id)
            self._unlock_dependents(db, task_id)
            return self._task(row)

    def expire_exhausted(self, task_id: str, *, expected_version: int) -> AgentTask:
        with self.sessions.begin() as db:
            row = db.get(AgentTaskRow, task_id)
            if row is None:
                raise TaskNotFoundError("Task does not exist.")
            if row.version != expected_version or row.status not in {
                TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value} or (
                row.lease_until is None or _utc(row.lease_until) > self.clock.now()):
                raise LeaseConflictError("Task lease is active or version is stale.")
            if row.attempt_count < row.max_attempts:
                raise TaskConflictError("Task has a remaining attempt.")
            now = self.clock.now()
            old_attempt = row.active_attempt_id
            row.status, row.active_attempt_id, row.lease_until = TaskStatus.FAILED.value, None, None
            row.error_code = "attempt_limit_reached"
            row.version += 1
            row.updated_at = row.completed_at = now
            if old_attempt:
                attempt = db.get(AgentTaskAttemptRow, old_attempt)
                if attempt:
                    attempt.status, attempt.completed_at, attempt.error_code = "expired", now, row.error_code
                self._event(db, row, TaskEventType.LEASE_EXPIRED, attempt_id=old_attempt)
            self._event(db, row, TaskEventType.TASK_FAILED, payload={"error_code": row.error_code})
            self._unlock_dependents(db, task_id)
            return self._task(row)

    def _owned(self, db: Session, task_id: str, attempt_id: str, statuses: set[TaskStatus]) -> AgentTaskRow:
        row = db.get(AgentTaskRow, task_id)
        if row is None:
            raise TaskNotFoundError("Task does not exist.")
        if row.active_attempt_id != attempt_id or row.status not in {s.value for s in statuses}:
            raise LeaseConflictError("Attempt no longer owns this task.")
        if row.lease_until and _utc(row.lease_until) <= self.clock.now():
            raise LeaseConflictError("Task claim expired.")
        if row.deadline_at and _utc(row.deadline_at) <= self.clock.now():
            raise TaskConflictError("Task deadline has passed.")
        return row

    def _unlock_dependents(self, db: Session, completed_id: str) -> None:
        dependents = db.scalars(select(AgentTaskDependencyRow).where(
            AgentTaskDependencyRow.depends_on_task_id == completed_id)).all()
        for dep in dependents:
            row = db.get(AgentTaskRow, dep.task_id)
            if row.status != TaskStatus.BLOCKED.value:
                continue
            all_deps = db.scalars(select(AgentTaskDependencyRow).where(
                AgentTaskDependencyRow.task_id == row.task_id)).all()
            states = [(DependencyType(item.dependency_type),
                TaskStatus.FAILED if (source.status == TaskStatus.BLOCKED.value
                    and source.error_code == "dependency_failed") else TaskStatus(source.status))
                for item in all_deps
                for source in [db.get(AgentTaskRow, item.depends_on_task_id)]]
            ready, blocked = dependency_ready(states)
            if ready:
                mappings = json.loads(row.input_spec_json).get("input_from_tasks", {})
                for role, source_key in mappings.items():
                    source_row = db.scalar(select(AgentTaskRow).where(
                        AgentTaskRow.root_task_id == row.root_task_id,
                        AgentTaskRow.idempotency_key == source_key))
                    if source_row is None:
                        raise TaskConflictError("Mapped source task is unavailable.")
                    artifact = db.scalar(select(AgentTaskArtifactRow).where(
                        AgentTaskArtifactRow.task_id == source_row.task_id,
                        AgentTaskArtifactRow.role == role))
                    if artifact is None:
                        row.error_code = "required_output_missing"
                        return
                    if db.get(AgentTaskArtifactLinkRow, (row.task_id, artifact.artifact_id, ArtifactDirection.INPUT.value)) is None:
                        db.add(AgentTaskArtifactLinkRow(task_id=row.task_id, artifact_id=artifact.artifact_id,
                            direction=ArtifactDirection.INPUT.value, role=role, created_at=self.clock.now()))
                optional = json.loads(row.input_spec_json).get("optional_input_from_tasks", {})
                for role, source_key in optional.items():
                    source_row = db.scalar(select(AgentTaskRow).where(
                        AgentTaskRow.root_task_id == row.root_task_id,
                        AgentTaskRow.idempotency_key == source_key))
                    artifact = db.scalar(select(AgentTaskArtifactRow).where(
                        AgentTaskArtifactRow.task_id == source_row.task_id,
                        AgentTaskArtifactRow.role == role)) if source_row else None
                    if artifact and db.get(AgentTaskArtifactLinkRow,
                            (row.task_id, artifact.artifact_id, ArtifactDirection.INPUT.value)) is None:
                        db.add(AgentTaskArtifactLinkRow(task_id=row.task_id,
                            artifact_id=artifact.artifact_id,
                            direction=ArtifactDirection.INPUT.value, role=role,
                            created_at=self.clock.now()))
                row.status, row.version, row.updated_at = TaskStatus.READY.value, row.version + 1, self.clock.now()
                self._event(db, row, TaskEventType.TASK_READY)
            elif blocked:
                row.error_code = "dependency_failed"
                self._unlock_dependents(db, row.task_id)

    def events(self, task_id: str) -> list[AgentTaskEvent]:
        self.require(task_id)
        with self.sessions() as db:
            return [AgentTaskEvent(event_id=r.event_id, task_id=r.task_id, sequence=r.sequence,
                event_type=r.event_type, attempt_id=r.attempt_id, payload=json.loads(r.payload_json),
                occurred_at=_utc(r.occurred_at)) for r in db.scalars(select(AgentTaskEventRow).where(
                    AgentTaskEventRow.task_id == task_id).order_by(AgentTaskEventRow.sequence))]

    def artifact_links(self, task_id: str, *, direction: ArtifactDirection | None = None) -> list[dict]:
        self.require(task_id)
        with self.sessions() as db:
            query = select(AgentTaskArtifactLinkRow).where(AgentTaskArtifactLinkRow.task_id == task_id)
            if direction:
                query = query.where(AgentTaskArtifactLinkRow.direction == direction.value)
            return [{"artifact_id": r.artifact_id, "role": r.role, "direction": r.direction}
                    for r in db.scalars(query.order_by(AgentTaskArtifactLinkRow.created_at))]

    def input_artifacts(self, task_id: str) -> dict[str, dict]:
        """Read only explicitly linked generic task outputs; never sibling conversations."""
        with self.sessions() as db:
            task = db.get(AgentTaskRow, task_id)
            if task is None:
                raise TaskNotFoundError("Task does not exist.")
            links = db.scalars(select(AgentTaskArtifactLinkRow).where(
                AgentTaskArtifactLinkRow.task_id == task_id,
                AgentTaskArtifactLinkRow.direction == ArtifactDirection.INPUT.value)).all()
            result: dict[str, dict] = {}
            for link in links:
                artifact = db.get(AgentTaskArtifactRow, link.artifact_id)
                if artifact is not None:
                    source_task = db.get(AgentTaskRow, artifact.task_id)
                    if source_task.application_id != task.application_id or source_task.parent_session_id != task.parent_session_id:
                        raise TaskConflictError("Linked task output belongs to another scope.")
                    result[link.artifact_id] = json.loads(artifact.content_json)
                    continue
                application_artifact = db.get(ApplicationArtifactRow, link.artifact_id)
                if application_artifact is None or application_artifact.application_id != task.application_id:
                    raise TaskConflictError("An input artifact is unavailable or out of scope.")
                result[link.artifact_id] = json.loads(application_artifact.content_json)
            return result

    def artifact_exists(self, artifact_id: str, *, application_id: str | None,
                        parent_session_id: str) -> bool:
        with self.sessions() as db:
            generic = db.get(AgentTaskArtifactRow, artifact_id)
            if generic is not None:
                source_task = db.get(AgentTaskRow, generic.task_id)
                return (source_task.application_id == application_id
                        and source_task.parent_session_id == parent_session_id)
            application_artifact = db.get(ApplicationArtifactRow, artifact_id)
            return (application_artifact is not None and application_id is not None
                    and application_artifact.application_id == application_id)

    def cancel(self, task_id: str, *, expected_version: int, subtree: bool = False) -> AgentTask:
        with self.sessions.begin() as db:
            root = db.get(AgentTaskRow, task_id)
            if root is None:
                raise TaskNotFoundError("Task does not exist.")
            if root.version != expected_version:
                raise TaskConflictError("Task version is stale.")
            ids = [task_id]
            subtree = subtree or root.parent_task_id is None
            if subtree:
                index = 0
                while index < len(ids):
                    ids.extend(r.task_id for r in db.scalars(select(AgentTaskRow).where(
                        AgentTaskRow.parent_task_id == ids[index])))
                    index += 1
            now = self.clock.now()
            for target in ids:
                row = db.get(AgentTaskRow, target)
                if row.status in TERMINAL:
                    continue
                if row.status == TaskStatus.AWAITING_APPROVAL.value:
                    self._deny_pending_approvals(db, target, now, "task_cancelled")
                if row.status in {TaskStatus.RUNNING.value, TaskStatus.CLAIMED.value}:
                    row.status = TaskStatus.CANCEL_REQUESTED.value
                    self._event(db, row, TaskEventType.CANCEL_REQUESTED)
                else:
                    row.status = TaskStatus.CANCELLED.value
                    row.completed_at = now
                    self._event(db, row, TaskEventType.TASK_CANCELLED)
                row.version += 1
                row.updated_at = now
            return self._task(root)

    @staticmethod
    def _deny_pending_approvals(db: Session, task_id: str, now: datetime, reason: str) -> None:
        pending = db.scalars(select(ToolCallRow).where(
            ToolCallRow.task_id == task_id,
            ToolCallRow.status == "approval_required")).all()
        for call in pending:
            call.status = "denied"
            call.error_code = reason
            call.error_message = "The task no longer permits this tool call."
            call.version += 1
            call.event_sequence += 1
            call.updated_at = now
            db.add(ToolCallEventRow(event_id=str(uuid4()), call_id=call.call_id,
                sequence=call.event_sequence, event_type=reason,
                from_status="approval_required", to_status="denied",
                payload_json="{}", occurred_at=now))

    def finish_cancel(self, task_id: str, attempt_id: str) -> AgentTask:
        with self.sessions.begin() as db:
            row = db.get(AgentTaskRow, task_id)
            if row is None or row.active_attempt_id != attempt_id or row.status != TaskStatus.CANCEL_REQUESTED.value:
                raise LeaseConflictError("Cancellation attempt is stale.")
            now = self.clock.now()
            row.status = TaskStatus.CANCELLED.value
            row.active_attempt_id = None
            row.lease_until = None
            row.completed_at = row.updated_at = now
            row.version += 1
            attempt = db.get(AgentTaskAttemptRow, attempt_id)
            attempt.status, attempt.completed_at = "cancelled", now
            if attempt.context_snapshot_id:
                snapshot = db.get(ContextSnapshotRow, attempt.context_snapshot_id)
                if snapshot and snapshot.status == "prepared":
                    snapshot.status, snapshot.abandoned_at, snapshot.version = "abandoned", now, snapshot.version + 1
            self._event(db, row, TaskEventType.TASK_CANCELLED, attempt_id=attempt_id)
            return self._task(row)

    def retry(self, task_id: str, *, expected_version: int) -> AgentTask:
        with self.sessions.begin() as db:
            row = db.get(AgentTaskRow, task_id)
            if row is None:
                raise TaskNotFoundError("Task does not exist.")
            if row.version != expected_version or row.status != TaskStatus.FAILED.value:
                raise TaskConflictError("Only a failed task at the current version may be retried.")
            if row.attempt_count >= row.max_attempts:
                raise BudgetExceededError("Task attempt limit reached.")
            row.status, row.error_code, row.completed_at = TaskStatus.READY.value, None, None
            row.version += 1
            row.updated_at = self.clock.now()
            self._event(db, row, TaskEventType.RETRY_SCHEDULED)
            return self._task(row)

    def pause(self, task_id: str, attempt_id: str, *, approval: bool,
              summary: str = "", pause_metadata: dict[str, str] | None = None) -> AgentTask:
        with self.sessions.begin() as db:
            row = self._owned(db, task_id, attempt_id, {TaskStatus.RUNNING})
            row.status = (TaskStatus.AWAITING_APPROVAL if approval else TaskStatus.AWAITING_INPUT).value
            row.result_summary_json = canonical_json({"summary": summary,
                "pause_metadata": pause_metadata or {}})
            row.active_attempt_id = None
            row.lease_until = None
            row.version += 1
            row.updated_at = self.clock.now()
            attempt = db.get(AgentTaskAttemptRow, attempt_id)
            attempt.status, attempt.completed_at = "paused", self.clock.now()
            if attempt.context_snapshot_id:
                snapshot = db.get(ContextSnapshotRow, attempt.context_snapshot_id)
                if snapshot and snapshot.status == "prepared":
                    snapshot.status, snapshot.used_at, snapshot.version = "used", self.clock.now(), snapshot.version + 1
            if approval:
                self._event(db, row, TaskEventType.TOOL_APPROVAL_REQUIRED, attempt_id=attempt_id)
            self._event(db, row, TaskEventType.TASK_PAUSED, attempt_id=attempt_id)
            return self._task(row)

    def resume(self, task_id: str, *, expected_version: int,
               continue_without_clarification: bool = False) -> AgentTask:
        with self.sessions.begin() as db:
            row = db.get(AgentTaskRow, task_id)
            if row is None:
                raise TaskNotFoundError("Task does not exist.")
            if row.version != expected_version or row.status not in {
                TaskStatus.AWAITING_APPROVAL.value, TaskStatus.AWAITING_INPUT.value}:
                raise TaskConflictError("Only a paused task at the current version may resume.")
            if row.attempt_count >= row.max_attempts:
                raise BudgetExceededError("Task attempt limit reached.")
            if continue_without_clarification:
                if row.task_type != "evidence_interview" or row.status != TaskStatus.AWAITING_INPUT.value:
                    raise TaskConflictError("Only a paused evidence interview may be skipped.")
                summary = json.loads(row.result_summary_json or "{}")
                summary.setdefault("pause_metadata", {})["continue_without_clarification"] = "true"
                row.result_summary_json = canonical_json(summary)
            row.status, row.version, row.updated_at = TaskStatus.READY.value, row.version + 1, self.clock.now()
            self._event(db, row, TaskEventType.TASK_RESUMED)
            return self._task(row)
