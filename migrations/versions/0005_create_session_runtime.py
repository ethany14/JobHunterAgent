"""Create versioned session snapshots, messages, and events.

Revision ID: 0005_session_runtime
Revises: 0004_backend_cutover
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_session_runtime"
down_revision = "0004_backend_cutover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("title", sa.String(256), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("message_sequence", sa.Integer(), nullable=False),
        sa.Column("active_run_id", sa.String(128), nullable=True),
        sa.Column("pending_tool_call_ids_json", sa.Text(), nullable=False),
        sa.Column("allowed_tools_json", sa.Text(), nullable=False),
        sa.Column("loop_iteration", sa.Integer(), nullable=False),
        sa.Column("executed_tool_calls", sa.Integer(), nullable=False),
        sa.Column("total_input_tokens", sa.Integer(), nullable=False),
        sa.Column("total_output_tokens", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','running','awaiting_user','awaiting_tool_approval',"
            "'completed','failed','cancelled')",
            name="ck_agent_sessions_status",
        ),
    )
    op.create_index("ix_agent_sessions_user_id", "agent_sessions", ["user_id"])
    op.create_index("ix_agent_sessions_status", "agent_sessions", ["status"])
    op.create_index("ix_agent_sessions_active_run_id", "agent_sessions", ["active_run_id"])

    op.create_table(
        "agent_session_messages",
        sa.Column("message_id", sa.String(128), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(128),
            sa.ForeignKey("agent_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(128), nullable=True),
        sa.Column("visibility", sa.String(24), nullable=False),
        sa.Column("message_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "session_id", "sequence", name="uq_session_message_sequence"
        ),
        sa.CheckConstraint(
            "visibility IN ('session','shared','task_private')",
            name="ck_session_message_visibility",
        ),
    )
    op.create_index(
        "ix_agent_session_messages_session_id",
        "agent_session_messages",
        ["session_id"],
    )
    op.create_index(
        "ix_agent_session_messages_task_id",
        "agent_session_messages",
        ["task_id"],
    )

    op.create_table(
        "agent_session_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(128),
            sa.ForeignKey("agent_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "session_id", "sequence", name="uq_session_event_sequence"
        ),
    )
    op.create_index(
        "ix_agent_session_events_session_id",
        "agent_session_events",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_session_events_session_id", table_name="agent_session_events")
    op.drop_table("agent_session_events")
    op.drop_index("ix_agent_session_messages_task_id", table_name="agent_session_messages")
    op.drop_index("ix_agent_session_messages_session_id", table_name="agent_session_messages")
    op.drop_table("agent_session_messages")
    op.drop_index("ix_agent_sessions_active_run_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_status", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_user_id", table_name="agent_sessions")
    op.drop_table("agent_sessions")
