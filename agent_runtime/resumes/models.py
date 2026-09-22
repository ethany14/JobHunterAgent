"""SQLAlchemy model for server-side resume documents."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class ResumeDocumentRow(Base):
    __tablename__ = "resume_documents"
    __table_args__ = (
        UniqueConstraint("owner_id", "content_sha256", name="uq_resume_owner_content"),
    )

    resume_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    extracted_text: Mapped[str] = mapped_column(Text, nullable=False)
    page_count: Mapped[int] = mapped_column(nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
