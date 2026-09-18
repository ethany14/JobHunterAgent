"""SQLAlchemy persistence for immutable context snapshot manifests."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class ContextSnapshotRow(Base):
    __tablename__ = "context_snapshots"
    __table_args__ = (
        CheckConstraint("status IN ('prepared','used','abandoned')", name="ck_context_snapshot_status"),
    )
    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.session_id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abandoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ContextSnapshotEventRow(Base):
    __tablename__ = "context_snapshot_events"
    __table_args__ = (UniqueConstraint("snapshot_id", "sequence", name="uq_context_snapshot_event_sequence"),)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("context_snapshots.snapshot_id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryUsageEventRow(Base):
    __tablename__ = "memory_usage_events"
    usage_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    memory_id: Mapped[str] = mapped_column(ForeignKey("memory_items.memory_id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("context_snapshots.snapshot_id", ondelete="CASCADE"), nullable=False, index=True)
    memory_version: Mapped[int] = mapped_column(Integer, nullable=False)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillUsageEventRow(Base):
    __tablename__ = "skill_usage_events"
    usage_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("skill_versions.version_id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("context_snapshots.snapshot_id", ondelete="CASCADE"), nullable=False, index=True)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
