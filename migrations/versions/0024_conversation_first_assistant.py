"""Add stable Assistant timeline and registered action requests.

Revision ID: 0024_conversation_first_assistant
Revises: 0023_governed_skill_evolution
"""
from alembic import op
import sqlalchemy as sa

revision = "0024_conversation_first_assistant"
down_revision = "0023_governed_skill_evolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("assistant_activities",
        sa.Column("activity_id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(128),
                  sa.ForeignKey("agent_sessions.session_id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("activity_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("reference_type", sa.String(64), nullable=False),
        sa.Column("reference_id", sa.String(128), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "sequence", name="uq_assistant_activity_sequence"),
        sa.UniqueConstraint("session_id", "reference_type", "reference_id",
                            name="uq_assistant_activity_reference"))
    op.create_index("ix_assistant_activities_session_id", "assistant_activities", ["session_id"])
    op.create_table("assistant_action_requests",
        sa.Column("action_id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(128),
                  sa.ForeignKey("agent_sessions.session_id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_type", sa.String(48), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reference_type", sa.String(64)),
        sa.Column("reference_id", sa.String(128)),
        sa.Column("safe_result_json", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "idempotency_key", name="uq_assistant_action_key"))
    op.create_index("ix_assistant_action_requests_session_id", "assistant_action_requests", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_assistant_action_requests_session_id", table_name="assistant_action_requests")
    op.drop_table("assistant_action_requests")
    op.drop_index("ix_assistant_activities_session_id", table_name="assistant_activities")
    op.drop_table("assistant_activities")
