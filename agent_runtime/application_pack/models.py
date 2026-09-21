"""Alembic-managed Application Pack projections and immutable evidence manifests."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class ApplicationPackRow(Base):
    __tablename__ = "application_packs"
    pack_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="single_custom")
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.application_id"), nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("job_snapshots.snapshot_id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    generation_number: Mapped[int] = mapped_column(Integer, nullable=False)
    record_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preference_snapshot_id: Mapped[str | None] = mapped_column(String(36))
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    stale_reason: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("application_id", "generation_number", name="uq_pack_application_version"),
        UniqueConstraint("application_id", "idempotency_key", name="uq_pack_idempotency"),
    )


class ApplicationPackItemRow(Base):
    __tablename__ = "application_pack_items"
    pack_item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pack_id: Mapped[str] = mapped_column(ForeignKey("application_packs.pack_id"), nullable=False, index=True)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("application_artifacts.artifact_id"))
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_question: Mapped[str | None] = mapped_column(Text)
    max_length: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    revision_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_revisions: Mapped[int] = mapped_column(Integer, nullable=False)
    verification_json: Mapped[str | None] = mapped_column(Text)
    requires_manual_answer: Mapped[bool] = mapped_column(Boolean, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("pack_id", "idempotency_key", name="uq_pack_item_idempotency"),)


class GenerationEvidenceSnapshotRow(Base):
    __tablename__ = "generation_evidence_snapshots"
    generation_snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pack_id: Mapped[str] = mapped_column(ForeignKey("application_packs.pack_id"), nullable=False, unique=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.application_id"), nullable=False)
    job_snapshot_id: Mapped[str] = mapped_column(ForeignKey("job_snapshots.snapshot_id"), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_set_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GenerationEvidenceSnapshotItemRow(Base):
    __tablename__ = "generation_evidence_snapshot_items"
    generation_snapshot_id: Mapped[str] = mapped_column(ForeignKey("generation_evidence_snapshots.generation_snapshot_id"), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    evidence_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    selection_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    associated_requirement_ids_json: Mapped[str] = mapped_column(Text, nullable=False)


class ApplicationPackEventRow(Base):
    __tablename__ = "application_pack_events"
    __table_args__ = (
        UniqueConstraint("pack_id", "sequence", name="uq_pack_event_sequence"),
        UniqueConstraint("pack_id", "idempotency_key", name="uq_pack_event_idempotency"),
    )
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pack_id: Mapped[str] = mapped_column(ForeignKey("application_packs.pack_id"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
