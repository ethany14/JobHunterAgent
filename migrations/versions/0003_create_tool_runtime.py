"""Create persistent tool-call audit tables.

Revision ID: 0003_tool_runtime
Revises: 0002_custom_agent
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_tool_runtime"
down_revision = "0002_custom_agent"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("tool_calls",
        sa.Column("call_id", sa.String(36), primary_key=True),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(128), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("tool_version", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("arguments_json", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("timeout_seconds", sa.Float(), nullable=True),
        sa.Column("approval_tool_name", sa.String(128), nullable=True),
        sa.Column("approval_tool_version", sa.String(64), nullable=True),
        sa.Column("approval_arguments_hash", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("event_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scope_type", "scope_id", "tool_name", "idempotency_key", name="uq_tool_call_idempotency"),
        sa.CheckConstraint("status IN ('requested','approval_required','approved','running','completed','failed','denied','timed_out','outcome_unknown')", name="ck_tool_calls_status"))
    op.create_table("tool_call_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("call_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["call_id"], ["tool_calls.call_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("call_id", "sequence", name="uq_tool_call_event_sequence"))
    op.create_index("ix_tool_call_events_call_id", "tool_call_events", ["call_id"])

def downgrade() -> None:
    op.drop_index("ix_tool_call_events_call_id", table_name="tool_call_events")
    op.drop_table("tool_call_events")
    op.drop_table("tool_calls")
