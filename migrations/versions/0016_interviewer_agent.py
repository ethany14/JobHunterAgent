"""Application-scoped Interviewer sessions, assessments and immutable turns.

Revision ID: 0016_interviewer_agent
Revises: 0015_career_evidence
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_interviewer_agent"
down_revision = "0015_career_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "application_requirement_assessments",
        sa.Column("assessment_id", sa.String(36), primary_key=True),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.application_id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("job_snapshots.snapshot_id"), nullable=False),
        sa.Column("requirement_id", sa.String(128), nullable=False),
        sa.Column("canonical_requirement", sa.String(256), nullable=False),
        sa.Column("original_requirement_text", sa.Text(), nullable=False),
        sa.Column("requirement_level", sa.String(16), nullable=False),
        sa.Column("match_status", sa.String(16), nullable=False),
        sa.Column("evidence_status", sa.String(32), nullable=False),
        sa.Column("linked_evidence_ids_json", sa.Text(), nullable=False),
        sa.Column("source_match_artifact_id", sa.String(36), sa.ForeignKey("application_artifacts.artifact_id"), nullable=False),
        sa.Column("jd_order", sa.Integer(), nullable=False),
        sa.Column("interview_exhausted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("application_id", "snapshot_id", "requirement_id", name="uq_app_requirement_snapshot"),
    )
    op.create_index("ix_application_requirement_assessments_application_id", "application_requirement_assessments", ["application_id"])
    op.create_index("ix_application_requirement_assessments_snapshot_id", "application_requirement_assessments", ["snapshot_id"])
    op.create_table(
        "interview_sessions",
        sa.Column("interview_session_id", sa.String(36), primary_key=True),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.application_id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("job_snapshots.snapshot_id"), nullable=False),
        sa.Column("agent_session_id", sa.String(36), sa.ForeignKey("agent_sessions.session_id"), nullable=False, unique=True),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("current_assessment_id", sa.String(36), sa.ForeignKey("application_requirement_assessments.assessment_id")),
        sa.Column("pending_answer_turn_id", sa.String(36)),
        sa.Column("pending_candidate_evidence_id", sa.String(36), sa.ForeignKey("career_evidence.evidence_id")),
        sa.Column("questions_asked", sa.Integer(), nullable=False),
        sa.Column("max_questions", sa.Integer(), nullable=False),
        sa.Column("followups_for_current_requirement", sa.Integer(), nullable=False),
        sa.Column("max_followups_per_requirement", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("turn_sequence", sa.Integer(), nullable=False),
        sa.Column("last_action_key", sa.String(128)),
        sa.Column("last_action_kind", sa.String(40)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('planning','awaiting_answer','awaiting_evidence_confirmation','completed','cancelled','failed')", name="ck_interview_status"),
    )
    op.create_index("ix_interview_sessions_application_id", "interview_sessions", ["application_id"])
    op.create_index("ix_interview_sessions_status", "interview_sessions", ["status"])
    op.create_index("uq_interview_active_per_application", "interview_sessions", ["application_id"], unique=True,
                    sqlite_where=sa.text("status IN ('planning','awaiting_answer','awaiting_evidence_confirmation','failed')"))
    op.create_table(
        "interview_turns",
        sa.Column("turn_id", sa.String(36), primary_key=True),
        sa.Column("interview_session_id", sa.String(36), sa.ForeignKey("interview_sessions.interview_session_id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("assessment_id", sa.String(36), sa.ForeignKey("application_requirement_assessments.assessment_id")),
        sa.Column("turn_type", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("interview_session_id", "sequence", name="uq_interview_turn_sequence"),
        sa.UniqueConstraint("interview_session_id", "idempotency_key", name="uq_interview_turn_idempotency"),
    )
    op.create_index("ix_interview_turns_interview_session_id", "interview_turns", ["interview_session_id"])
    op.execute("CREATE TRIGGER interview_turns_no_update BEFORE UPDATE ON interview_turns BEGIN SELECT RAISE(ABORT, 'interview turns are immutable'); END")
    op.execute("CREATE TRIGGER interview_turns_no_delete BEFORE DELETE ON interview_turns BEGIN SELECT RAISE(ABORT, 'interview turns are immutable'); END")


def downgrade() -> None:
    op.execute("DROP TRIGGER interview_turns_no_update")
    op.execute("DROP TRIGGER interview_turns_no_delete")
    op.drop_table("interview_turns")
    op.drop_table("interview_sessions")
    op.drop_table("application_requirement_assessments")
