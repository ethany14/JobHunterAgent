"""Task, attempt, dependency, artifact and audit rows."""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class AgentPlanRow(Base):
    __tablename__ = "agent_plans"
    __table_args__ = (UniqueConstraint("parent_session_id", "idempotency_key", name="uq_agent_plan_scope_key"),)
    plan_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="single_custom")
    parent_session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.session_id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    root_task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    budget_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentTaskRow(Base):
    __tablename__ = "agent_tasks"
    __table_args__ = (UniqueConstraint("parent_session_id", "parent_task_id", "idempotency_key", name="uq_agent_task_parent_key"),)
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_role: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_task_id: Mapped[str | None] = mapped_column(ForeignKey("agent_tasks.task_id"), index=True)
    root_task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    parent_session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.session_id"), nullable=False, index=True)
    child_session_id: Mapped[str | None] = mapped_column(ForeignKey("agent_sessions.session_id"), index=True)
    application_id: Mapped[str | None] = mapped_column(ForeignKey("applications.application_id"), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    active_attempt_id: Mapped[str | None] = mapped_column(String(36), index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    input_spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    output_spec_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_summary_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentTaskDependencyRow(Base):
    __tablename__ = "agent_task_dependencies"
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.task_id"), primary_key=True)
    depends_on_task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.task_id"), primary_key=True)
    dependency_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (CheckConstraint("task_id != depends_on_task_id", name="ck_task_no_self_dependency"),)


class AgentTaskArtifactLinkRow(Base):
    __tablename__ = "agent_task_artifact_links"
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.task_id"), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    direction: Mapped[str] = mapped_column(String(8), primary_key=True)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentTaskEventRow(Base):
    __tablename__ = "agent_task_events"
    __table_args__ = (UniqueConstraint("task_id", "sequence", name="uq_agent_task_event_sequence"),)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.task_id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    attempt_id: Mapped[str | None] = mapped_column(String(36))
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentTaskAttemptRow(Base):
    __tablename__ = "agent_task_attempts"
    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.task_id"), nullable=False, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    context_snapshot_id: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    usage_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("task_id", "attempt_number", name="uq_task_attempt_number"),)


class AgentTaskArtifactRow(Base):
    """Immutable generic output for infrastructure workers; separate from ApplicationArtifact."""
    __tablename__ = "agent_task_artifacts"
    artifact_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="single_custom")
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.task_id"), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(String(36), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("task_id", "role", name="uq_task_output_role"),)
