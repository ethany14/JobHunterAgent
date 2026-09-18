"""Add session and tool execution claims.

Revision ID: 0007_execution_claims
Revises: 0006_session_resumability
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_execution_claims"
down_revision = "0006_session_resumability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("active_attempt_id", sa.String(36)))
    op.add_column("agent_sessions", sa.Column("lease_until", sa.DateTime(timezone=True)))
    op.create_index("ix_agent_sessions_active_attempt_id", "agent_sessions", ["active_attempt_id"])
    op.create_index("ix_agent_sessions_lease_until", "agent_sessions", ["lease_until"])
    op.create_table(
        "agent_session_attempts",
        sa.Column("attempt_id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(128), sa.ForeignKey("agent_sessions.session_id", ondelete="CASCADE"), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("recovered_from_attempt_id", sa.String(36)),
        sa.CheckConstraint("status IN ('active','released','expired')", name="ck_session_attempt_status"),
    )
    op.create_index("ix_agent_session_attempts_session_id", "agent_session_attempts", ["session_id"])
    op.create_index("ix_agent_session_attempts_worker_id", "agent_session_attempts", ["worker_id"])
    op.create_index("ix_agent_session_attempts_status", "agent_session_attempts", ["status"])
    op.add_column("tool_calls", sa.Column("tool_side_effect", sa.String(32)))
    op.add_column("tool_calls", sa.Column("tool_idempotent", sa.Boolean()))
    op.add_column("tool_calls", sa.Column("execution_attempt_id", sa.String(36)))
    op.add_column("tool_calls", sa.Column("execution_lease_until", sa.DateTime(timezone=True)))
    op.create_index("ix_tool_calls_execution_attempt_id", "tool_calls", ["execution_attempt_id"])
    op.create_index("ix_tool_calls_execution_lease_until", "tool_calls", ["execution_lease_until"])


def downgrade() -> None:
    op.drop_index("ix_tool_calls_execution_lease_until", table_name="tool_calls")
    op.drop_index("ix_tool_calls_execution_attempt_id", table_name="tool_calls")
    with op.batch_alter_table("tool_calls") as batch:
        batch.drop_column("execution_lease_until")
        batch.drop_column("execution_attempt_id")
        batch.drop_column("tool_idempotent")
        batch.drop_column("tool_side_effect")
    op.drop_table("agent_session_attempts")
    op.drop_index("ix_agent_sessions_lease_until", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_active_attempt_id", table_name="agent_sessions")
    with op.batch_alter_table("agent_sessions") as batch:
        batch.drop_column("lease_until")
        batch.drop_column("active_attempt_id")
