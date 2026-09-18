"""Add persisted pending-decision and loop-limit fields to sessions.

Revision ID: 0006_session_resumability
Revises: 0005_session_runtime
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_session_resumability"
down_revision = "0005_session_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_sessions",
        sa.Column("pending_assistant_message_id", sa.String(128), nullable=True),
    )
    op.add_column(
        "agent_sessions",
        sa.Column("max_loop_iterations", sa.Integer(), nullable=False, server_default="6"),
    )
    op.add_column(
        "agent_sessions",
        sa.Column("max_tool_calls", sa.Integer(), nullable=False, server_default="10"),
    )


def downgrade() -> None:
    with op.batch_alter_table("agent_sessions") as batch:
        batch.drop_column("max_tool_calls")
        batch.drop_column("max_loop_iterations")
        batch.drop_column("pending_assistant_message_id")
