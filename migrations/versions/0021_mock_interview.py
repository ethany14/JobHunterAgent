"""Persistent mock interviews, immutable turns, plans, reports and audit.

Revision ID: 0021_mock_interview
Revises: 0020_interview_match_cohorts
"""
from alembic import op
import sqlalchemy as sa

revision = "0021_mock_interview"
down_revision = "0020_interview_match_cohorts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("mock_interviews",
        sa.Column("mock_interview_id", sa.String(36), primary_key=True),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.application_id"), nullable=False),
        sa.Column("root_task_id", sa.String(36)),
        sa.Column("agent_session_id", sa.String(36), sa.ForeignKey("agent_sessions.session_id"), nullable=False, unique=True),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("difficulty", sa.String(24), nullable=False),
        sa.Column("target_question_count", sa.Integer(), nullable=False),
        sa.Column("questions_completed", sa.Integer(), nullable=False),
        sa.Column("max_followups_per_question", sa.Integer(), nullable=False),
        sa.Column("current_question_id", sa.String(36)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("answer_sequence", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)))
    op.create_index("ix_mock_interviews_application_id", "mock_interviews", ["application_id"])
    op.create_index("uq_mock_active_application_mode", "mock_interviews",
        ["application_id", "mode"], unique=True,
        sqlite_where=sa.text("status IN ('planning','awaiting_answer','evaluating','failed')"))
    op.create_table("mock_interview_plans",
        sa.Column("plan_id", sa.String(36), primary_key=True),
        sa.Column("mock_interview_id", sa.String(36), sa.ForeignKey("mock_interviews.mock_interview_id"), nullable=False, unique=True),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("mock_interview_plan_items",
        sa.Column("plan_item_id", sa.String(36), primary_key=True),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("mock_interview_plans.plan_id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.UniqueConstraint("plan_id", "sequence", name="uq_mock_plan_item_sequence"))
    op.create_table("mock_interview_questions",
        sa.Column("question_id", sa.String(36), primary_key=True),
        sa.Column("mock_interview_id", sa.String(36), sa.ForeignKey("mock_interviews.mock_interview_id"), nullable=False),
        sa.Column("plan_item_id", sa.String(36), sa.ForeignKey("mock_interview_plan_items.plan_item_id"), nullable=False),
        sa.Column("parent_question_id", sa.String(36)),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_mock_interview_questions_mock_interview_id", "mock_interview_questions", ["mock_interview_id"])
    op.create_table("mock_interview_answers",
        sa.Column("answer_id", sa.String(36), primary_key=True),
        sa.Column("mock_interview_id", sa.String(36), sa.ForeignKey("mock_interviews.mock_interview_id"), nullable=False),
        sa.Column("question_id", sa.String(36), sa.ForeignKey("mock_interview_questions.question_id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("mock_interview_id", "idempotency_key", name="uq_mock_answer_key"),
        sa.UniqueConstraint("question_id", name="uq_mock_question_answer"),
        sa.UniqueConstraint("mock_interview_id", "sequence", name="uq_mock_answer_sequence"))
    op.create_index("ix_mock_interview_answers_mock_interview_id", "mock_interview_answers", ["mock_interview_id"])
    op.create_table("mock_answer_evaluations",
        sa.Column("evaluation_id", sa.String(36), primary_key=True),
        sa.Column("answer_id", sa.String(36), sa.ForeignKey("mock_interview_answers.answer_id"), nullable=False, unique=True),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("mock_interview_reports",
        sa.Column("report_id", sa.String(36), primary_key=True),
        sa.Column("mock_interview_id", sa.String(36), sa.ForeignKey("mock_interviews.mock_interview_id"), nullable=False, unique=True),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.application_id"), nullable=False),
        sa.Column("artifact_type", sa.String(32), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("mock_interview_candidates",
        sa.Column("mock_interview_id", sa.String(36), sa.ForeignKey("mock_interviews.mock_interview_id"), primary_key=True),
        sa.Column("answer_id", sa.String(36), sa.ForeignKey("mock_interview_answers.answer_id"), primary_key=True),
        sa.Column("evidence_id", sa.String(36), sa.ForeignKey("career_evidence.evidence_id"), primary_key=True),
        sa.UniqueConstraint("answer_id", "evidence_id", name="uq_mock_candidate_answer"))
    op.create_table("mock_interview_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("mock_interview_id", sa.String(36), sa.ForeignKey("mock_interviews.mock_interview_id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("mock_interview_id", "sequence", name="uq_mock_event_sequence"),
        sa.UniqueConstraint("mock_interview_id", "idempotency_key", name="uq_mock_event_key"))


def downgrade() -> None:
    raise RuntimeError("Mock interview migration is forward-only.")
