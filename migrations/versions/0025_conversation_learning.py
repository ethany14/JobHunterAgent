"""Add governed conversation experiences and reusable learning patterns.

Revision ID: 0025_conversation_learning
Revises: 0024_conversation_first_assistant
"""
from alembic import op
import sqlalchemy as sa

revision = "0025_conversation_learning"
down_revision = "0024_conversation_first_assistant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_experiences",
        sa.Column("experience_id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(128),
                  sa.ForeignKey("agent_sessions.session_id", ondelete="CASCADE"), nullable=False),
        sa.Column("application_id", sa.String(36)),
        sa.Column("user_message_id", sa.String(128), nullable=False),
        sa.Column("assistant_message_id", sa.String(128), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("observation_json", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "user_message_id", "assistant_message_id",
                            name="uq_conversation_experience_turn"),
    )
    op.create_index("ix_conversation_experiences_owner_id", "conversation_experiences", ["owner_id"])
    op.create_index("ix_conversation_experiences_session_id", "conversation_experiences", ["session_id"])
    op.create_index("ix_conversation_experiences_application_id", "conversation_experiences", ["application_id"])
    op.create_table(
        "conversation_experience_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("experience_id", sa.String(36),
                  sa.ForeignKey("conversation_experiences.experience_id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("experience_id", "sequence", name="uq_conversation_experience_event"),
    )
    op.create_table(
        "conversation_learning_patterns",
        sa.Column("pattern_id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("canonical_key", sa.String(160), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("emitted_feedback_event_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "canonical_key", name="uq_conversation_pattern_key"),
    )
    op.create_index("ix_conversation_learning_patterns_owner_id",
                    "conversation_learning_patterns", ["owner_id"])
    op.create_table(
        "conversation_pattern_experiences",
        sa.Column("pattern_id", sa.String(36),
                  sa.ForeignKey("conversation_learning_patterns.pattern_id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("experience_id", sa.String(36),
                  sa.ForeignKey("conversation_experiences.experience_id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("Conversation learning migration is forward-only.")
