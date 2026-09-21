"""Governed feedback events and reviewable learning candidates.

Revision ID: 0022_feedback_learning
Revises: 0021_mock_interview
"""
from alembic import op
import sqlalchemy as sa

revision = "0022_feedback_learning"
down_revision = "0021_mock_interview"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("feedback_events",
        sa.Column("feedback_event_id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("application_id", sa.String(36)), sa.Column("session_id", sa.String(128)),
        sa.Column("task_id", sa.String(36)), sa.Column("artifact_id", sa.String(36)),
        sa.Column("artifact_version", sa.Integer()),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_action_id", sa.String(160), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("signal_strength", sa.String(16), nullable=False),
        sa.Column("processed_status", sa.String(40), nullable=False),
        sa.Column("processing_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("owner_id", "source_action_id", name="uq_feedback_source_action"))
    op.create_index("ix_feedback_events_owner_id", "feedback_events", ["owner_id"])
    op.create_index("ix_feedback_events_application_id", "feedback_events", ["application_id"])
    op.create_table("learning_candidates",
        sa.Column("candidate_id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("candidate_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("canonical_key", sa.String(160), nullable=False),
        sa.Column("scope", sa.String(24), nullable=False),
        sa.Column("scope_id", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_learning_candidates_owner_id", "learning_candidates", ["owner_id"])
    op.create_index("ix_learning_aggregate", "learning_candidates",
        ["owner_id", "candidate_type", "scope", "scope_id", "canonical_key"])
    op.create_table("learning_candidate_event_links",
        sa.Column("candidate_id", sa.String(36), sa.ForeignKey("learning_candidates.candidate_id"), primary_key=True),
        sa.Column("feedback_event_id", sa.String(36), sa.ForeignKey("feedback_events.feedback_event_id"), primary_key=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("learning_candidate_conflicts",
        sa.Column("candidate_id", sa.String(36), sa.ForeignKey("learning_candidates.candidate_id"), primary_key=True),
        sa.Column("conflicting_candidate_id", sa.String(36), sa.ForeignKey("learning_candidates.candidate_id"), primary_key=True),
        sa.Column("reason_code", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("feedback_processing_attempts",
        sa.Column("attempt_id", sa.String(36), primary_key=True),
        sa.Column("feedback_event_id", sa.String(36), sa.ForeignKey("feedback_events.feedback_event_id"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("classification_json", sa.Text()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("feedback_event_id", "attempt_number", name="uq_feedback_attempt_number"))
    op.create_index("ix_feedback_processing_attempts_feedback_event_id",
        "feedback_processing_attempts", ["feedback_event_id"])


def downgrade() -> None:
    raise RuntimeError("Feedback learning migration is forward-only.")
