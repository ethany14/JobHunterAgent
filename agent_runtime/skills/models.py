"""SQLAlchemy models for governed Skill versions and lifecycle events."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base


class SkillRow(Base):
    __tablename__ = "skill_registry"

    skill_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    active_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillVersionRow(Base):
    __tablename__ = "skill_versions"
    __table_args__ = (
        UniqueConstraint("skill_id", "version_label", name="uq_skill_version_label"),
        CheckConstraint(
            "status IN ('draft','validating','validated','approval_required','approved','active',"
            "'rejected','superseded','retired')",
            name="ck_skill_versions_status",
        ),
    )

    version_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    skill_id: Mapped[str] = mapped_column(
        ForeignKey("skill_registry.skill_id", ondelete="CASCADE"), nullable=False, index=True
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(1024), nullable=False)
    version_label: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    package_path: Mapped[str] = mapped_column(Text, nullable=False)
    license: Mapped[str | None] = mapped_column(String(512))
    compatibility: Mapped[str | None] = mapped_column(String(500))
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_tools_json: Mapped[str | None] = mapped_column(Text)
    instruction_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    validation_errors_json: Mapped[str] = mapped_column(Text, nullable=False)
    validation_warnings_json: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    version_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillEventRow(Base):
    __tablename__ = "skill_events"
    __table_args__ = (
        UniqueConstraint("version_id", "sequence", name="uq_skill_event_sequence"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("skill_versions.version_id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillEvaluationResultRow(Base):
    __tablename__ = "skill_evaluation_results"

    evaluation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("skill_versions.version_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    suite_name: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
