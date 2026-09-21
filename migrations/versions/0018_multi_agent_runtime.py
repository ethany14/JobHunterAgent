"""Persist deterministic multi-agent task graphs and attempts.

Revision ID: 0018_multi_agent_runtime
Revises: 0017_application_pack
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_multi_agent_runtime"
down_revision = "0017_application_pack"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("parent_session_id", sa.String(128), nullable=True))
    op.add_column("agent_sessions", sa.Column("task_id", sa.String(36), nullable=True))
    op.add_column("agent_sessions", sa.Column("agent_role", sa.String(64), nullable=True))
    op.create_index("ix_agent_sessions_parent_session_id", "agent_sessions", ["parent_session_id"])
    op.create_index("ix_agent_sessions_task_id", "agent_sessions", ["task_id"])
    op.add_column("tool_calls", sa.Column("task_id", sa.String(36), nullable=True))
    op.add_column("tool_calls", sa.Column("attempt_id", sa.String(36), nullable=True))
    op.create_index("ix_tool_calls_task_id", "tool_calls", ["task_id"])
    op.create_table("agent_plans",
        sa.Column("plan_id", sa.String(36), primary_key=True),
        sa.Column("parent_session_id", sa.String(128), sa.ForeignKey("agent_sessions.session_id"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("root_task_id", sa.String(36), nullable=False),
        sa.Column("budget_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("parent_session_id", "idempotency_key", name="uq_agent_plan_scope_key"))
    op.create_table("agent_tasks",
        sa.Column("task_id", sa.String(36), primary_key=True),
        sa.Column("task_type", sa.String(64), nullable=False), sa.Column("agent_role", sa.String(64), nullable=False),
        sa.Column("parent_task_id", sa.String(36), sa.ForeignKey("agent_tasks.task_id")),
        sa.Column("root_task_id", sa.String(36), nullable=False),
        sa.Column("parent_session_id", sa.String(128), sa.ForeignKey("agent_sessions.session_id"), nullable=False),
        sa.Column("child_session_id", sa.String(128), sa.ForeignKey("agent_sessions.session_id")),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.application_id")),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False), sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False), sa.Column("active_attempt_id", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True)), sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column("idempotency_key", sa.String(128), nullable=False), sa.Column("input_spec_json", sa.Text(), nullable=False),
        sa.Column("output_spec_json", sa.Text(), nullable=False), sa.Column("result_summary_json", sa.Text()),
        sa.Column("error_code", sa.String(64)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("parent_session_id", "parent_task_id", "idempotency_key", name="uq_agent_task_parent_key"))
    for column in ("root_task_id", "parent_session_id", "parent_task_id", "status", "active_attempt_id"):
        op.create_index(f"ix_agent_tasks_{column}", "agent_tasks", [column])
    op.create_table("agent_task_dependencies",
        sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_tasks.task_id"), primary_key=True),
        sa.Column("depends_on_task_id", sa.String(36), sa.ForeignKey("agent_tasks.task_id"), primary_key=True),
        sa.Column("dependency_type", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("task_id != depends_on_task_id", name="ck_task_no_self_dependency"))
    op.create_table("agent_task_artifact_links",
        sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_tasks.task_id"), primary_key=True),
        sa.Column("artifact_id", sa.String(36), primary_key=True), sa.Column("direction", sa.String(8), primary_key=True),
        sa.Column("role", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("agent_task_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_tasks.task_id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False), sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("attempt_id", sa.String(36)), sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "sequence", name="uq_agent_task_event_sequence"))
    op.create_index("ix_agent_task_events_task_id", "agent_task_events", ["task_id"])
    op.create_table("agent_task_attempts",
        sa.Column("attempt_id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_tasks.task_id"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False), sa.Column("status", sa.String(24), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False), sa.Column("context_snapshot_id", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)), sa.Column("usage_json", sa.Text()),
        sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint("task_id", "attempt_number", name="uq_task_attempt_number"))
    op.create_index("ix_agent_task_attempts_task_id", "agent_task_attempts", ["task_id"])
    op.create_table("agent_task_artifacts",
        sa.Column("artifact_id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_tasks.task_id"), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=False), sa.Column("role", sa.String(64), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "role", name="uq_task_output_role"))
    op.create_index("ix_agent_task_artifacts_task_id", "agent_task_artifacts", ["task_id"])


def downgrade() -> None:
    raise RuntimeError("Multi-Agent migration is forward-only.")
