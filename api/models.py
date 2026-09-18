"""SQLAlchemy models for the API persistence layer."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'awaiting_review', 'revising', 'approved', 'failed')",
            name="ck_runs_status",
        ),
        CheckConstraint(
            "backend IN ('custom', 'langgraph', 'unknown')",
            name="ck_runs_backend",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    backend: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown", index=True)
    backend_source: Mapped[str] = mapped_column(String(32), nullable=False, default="unclassified")
    resume_text: Mapped[str] = mapped_column(Text, nullable=False)
    job_description: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
