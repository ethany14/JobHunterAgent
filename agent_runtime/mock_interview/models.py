"""SQLAlchemy projections and immutable interview records."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class MockInterviewRow(Base):
    __tablename__ = "mock_interviews"
    __table_args__ = (Index("uq_mock_active_application_mode", "application_id", "mode",
        unique=True, sqlite_where=text("status IN ('planning','awaiting_answer','evaluating','failed')")),)
    mock_interview_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.application_id"), nullable=False, index=True)
    root_task_id: Mapped[str | None] = mapped_column(String(36))
    agent_session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.session_id"), nullable=False, unique=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    difficulty: Mapped[str] = mapped_column(String(24), nullable=False)
    target_question_count: Mapped[int] = mapped_column(Integer, nullable=False)
    questions_completed: Mapped[int] = mapped_column(Integer, nullable=False)
    max_followups_per_question: Mapped[int] = mapped_column(Integer, nullable=False)
    current_question_id: Mapped[str | None] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    answer_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))


class MockInterviewPlanRow(Base):
    __tablename__ = "mock_interview_plans"
    plan_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mock_interview_id: Mapped[str] = mapped_column(ForeignKey("mock_interviews.mock_interview_id"), nullable=False, unique=True)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MockInterviewPlanItemRow(Base):
    __tablename__ = "mock_interview_plan_items"
    __table_args__ = (UniqueConstraint("plan_id", "sequence", name="uq_mock_plan_item_sequence"),)
    plan_item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("mock_interview_plans.plan_id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)


class MockInterviewQuestionRow(Base):
    __tablename__ = "mock_interview_questions"
    question_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mock_interview_id: Mapped[str] = mapped_column(ForeignKey("mock_interviews.mock_interview_id"), nullable=False, index=True)
    plan_item_id: Mapped[str] = mapped_column(ForeignKey("mock_interview_plan_items.plan_item_id"), nullable=False)
    parent_question_id: Mapped[str | None] = mapped_column(String(36))
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MockInterviewAnswerRow(Base):
    __tablename__ = "mock_interview_answers"
    __table_args__ = (
        UniqueConstraint("mock_interview_id", "idempotency_key", name="uq_mock_answer_key"),
        UniqueConstraint("question_id", name="uq_mock_question_answer"),
        UniqueConstraint("mock_interview_id", "sequence", name="uq_mock_answer_sequence"),
    )
    answer_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mock_interview_id: Mapped[str] = mapped_column(ForeignKey("mock_interviews.mock_interview_id"), nullable=False, index=True)
    question_id: Mapped[str] = mapped_column(ForeignKey("mock_interview_questions.question_id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MockAnswerEvaluationRow(Base):
    __tablename__ = "mock_answer_evaluations"
    evaluation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    answer_id: Mapped[str] = mapped_column(ForeignKey("mock_interview_answers.answer_id"), nullable=False, unique=True)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MockInterviewReportRow(Base):
    __tablename__ = "mock_interview_reports"
    report_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mock_interview_id: Mapped[str] = mapped_column(ForeignKey("mock_interviews.mock_interview_id"), nullable=False, unique=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.application_id"), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False, default="interview_report")
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MockInterviewCandidateRow(Base):
    __tablename__ = "mock_interview_candidates"
    __table_args__ = (UniqueConstraint("answer_id", "evidence_id", name="uq_mock_candidate_answer"),)
    mock_interview_id: Mapped[str] = mapped_column(ForeignKey("mock_interviews.mock_interview_id"), primary_key=True)
    answer_id: Mapped[str] = mapped_column(ForeignKey("mock_interview_answers.answer_id"), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("career_evidence.evidence_id"), primary_key=True)


class MockInterviewEventRow(Base):
    __tablename__ = "mock_interview_events"
    __table_args__ = (
        UniqueConstraint("mock_interview_id", "sequence", name="uq_mock_event_sequence"),
        UniqueConstraint("mock_interview_id", "idempotency_key", name="uq_mock_event_key"),
    )
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mock_interview_id: Mapped[str] = mapped_column(ForeignKey("mock_interviews.mock_interview_id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
