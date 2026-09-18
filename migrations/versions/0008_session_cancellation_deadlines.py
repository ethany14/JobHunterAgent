"""Add session cancellation and deadline state.

Revision ID: 0008_session_cancellation_deadlines
Revises: 0007_execution_claims
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_session_cancellation_deadlines"
down_revision = "0007_execution_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_sessions") as batch:
        batch.drop_constraint("ck_agent_sessions_status", type_="check")
        batch.create_check_constraint(
            "ck_agent_sessions_status",
            "status IN ('active','running','awaiting_user','awaiting_tool_approval',"
            "'completed','failed','cancelled','timed_out')",
        )
        batch.add_column(sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("cancel_reason", sa.Text()))
        batch.add_column(sa.Column("turn_deadline_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("session_expires_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("terminal_reason", sa.String(128)))
        batch.add_column(sa.Column("manual_recovery_tool_call_ids_json", sa.Text(), nullable=False, server_default="[]"))
    op.create_index("ix_agent_sessions_turn_deadline_at", "agent_sessions", ["turn_deadline_at"])
    op.create_index("ix_agent_sessions_session_expires_at", "agent_sessions", ["session_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_session_expires_at", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_turn_deadline_at", table_name="agent_sessions")
    with op.batch_alter_table("agent_sessions") as batch:
        batch.drop_column("manual_recovery_tool_call_ids_json")
        batch.drop_column("terminal_reason")
        batch.drop_column("session_expires_at")
        batch.drop_column("turn_deadline_at")
        batch.drop_column("cancel_reason")
        batch.drop_column("cancel_requested_at")
        batch.drop_column("cancel_requested")
        batch.drop_constraint("ck_agent_sessions_status", type_="check")
        batch.create_check_constraint(
            "ck_agent_sessions_status",
            "status IN ('active','running','awaiting_user','awaiting_tool_approval',"
            "'completed','failed','cancelled')",
        )
