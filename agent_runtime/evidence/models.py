"""SQLite rows for immutable evidence versions and append-only audit events."""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class CareerEvidenceRow(Base):
    __tablename__ = "career_evidence"
    __table_args__ = (CheckConstraint("status IN ('candidate','confirmed','rejected','superseded','archived')", name="ck_career_evidence_status"),)
    evidence_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CareerEvidenceVersionRow(Base):
    __tablename__ = "career_evidence_versions"
    __table_args__ = (
        UniqueConstraint("evidence_id", "version_number", name="uq_career_evidence_version"),
        UniqueConstraint("source_type", "source_reference", "external_evidence_id", "content_hash", name="uq_career_evidence_import"),
    )
    evidence_version_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("career_evidence.evidence_id"), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(256))
    exact_quote: Mapped[str | None] = mapped_column(Text)
    source_section: Mapped[str | None] = mapped_column(String(256))
    employer_or_project: Mapped[str | None] = mapped_column(String(256))
    role: Mapped[str | None] = mapped_column(String(256))
    start_date: Mapped[str | None] = mapped_column(String(32))
    end_date: Mapped[str | None] = mapped_column(String(32))
    technologies_json: Mapped[str] = mapped_column(Text, nullable=False)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    external_evidence_id: Mapped[str | None] = mapped_column(String(64))
    source_run_id: Mapped[str | None] = mapped_column(String(36))


class CareerEvidenceEventRow(Base):
    __tablename__ = "career_evidence_events"
    __table_args__ = (UniqueConstraint("evidence_id", "sequence", name="uq_career_evidence_event_sequence"),)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("career_evidence.evidence_id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence_version_id: Mapped[str | None] = mapped_column(ForeignKey("career_evidence_versions.evidence_version_id"))
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EvidenceApplicationLinkRow(Base):
    __tablename__ = "evidence_application_links"
    __table_args__ = (UniqueConstraint("application_id", "evidence_version_id", "requirement_id", name="uq_evidence_application_requirement"),)
    link_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("career_evidence.evidence_id"), nullable=False, index=True)
    evidence_version_id: Mapped[str] = mapped_column(ForeignKey("career_evidence_versions.evidence_version_id"), nullable=False)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.application_id", ondelete="CASCADE"), nullable=False, index=True)
    requirement_id: Mapped[str | None] = mapped_column(String(128))
    canonical_requirement: Mapped[str | None] = mapped_column(String(256))
    link_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
