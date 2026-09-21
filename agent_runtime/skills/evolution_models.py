"""Additive governed-evolution tables; legacy Skill rows stay intact."""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from api.db import Base


class SkillEvolutionCandidateRow(Base):
    __tablename__ = "skill_evolution_candidates"
    candidate_id: Mapped[str] = mapped_column(ForeignKey("learning_candidates.candidate_id"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    staged_path: Mapped[str | None] = mapped_column(Text)
    staged_hash: Mapped[str | None] = mapped_column(String(64))
    skill_name: Mapped[str | None] = mapped_column(String(64))
    semantic_version: Mapped[str | None] = mapped_column(String(64))
    evaluation_run_id: Mapped[str | None] = mapped_column(String(36))
    published_version_id: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillEvolutionVersionRow(Base):
    __tablename__ = "skill_evolution_versions"
    __table_args__ = (UniqueConstraint("skill_name", "semantic_version"),)
    skill_version_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    skill_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    semantic_version: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(String(36))
    candidate_id: Mapped[str] = mapped_column(ForeignKey("learning_candidates.candidate_id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    activation_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    package_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    evaluation_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    published_by: Mapped[str] = mapped_column(String(128), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillEvaluationRunRow(Base):
    __tablename__ = "skill_evaluation_runs"
    evaluation_run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("learning_candidates.candidate_id"), nullable=False, index=True)
    baseline_skill_version_id: Mapped[str | None] = mapped_column(String(36))
    staged_skill_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    staged_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    temperature: Mapped[float] = mapped_column(Float, nullable=False)
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))


class SkillEvaluationCaseResultRow(Base):
    __tablename__ = "skill_evaluation_case_results"
    __table_args__ = (UniqueConstraint("evaluation_run_id", "case_id", "variant", "repetition"),)
    result_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    evaluation_run_id: Mapped[str] = mapped_column(ForeignKey("skill_evaluation_runs.evaluation_run_id"), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(128), nullable=False)
    variant: Mapped[str] = mapped_column(String(16), nullable=False)
    repetition: Mapped[int] = mapped_column(Integer, nullable=False)
    output_reference: Mapped[str | None] = mapped_column(String(128))
    output_text: Mapped[str | None] = mapped_column(Text)
    metric_values_json: Mapped[str] = mapped_column(Text, nullable=False)
    latency: Mapped[float] = mapped_column(Float, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    safety_violations: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillForwardTestResultRow(Base):
    __tablename__ = "skill_forward_test_results"
    result_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    evaluation_run_id: Mapped[str] = mapped_column(ForeignKey("skill_evaluation_runs.evaluation_run_id"), nullable=False)
    case_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_values_json: Mapped[str] = mapped_column(Text, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillActivationEventRow(Base):
    __tablename__ = "skill_activation_events"
    __table_args__ = (UniqueConstraint("candidate_id", "request_key"),)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    skill_version_id: Mapped[str | None] = mapped_column(String(36))
    candidate_id: Mapped[str | None] = mapped_column(String(36))
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(128))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    request_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SkillRuntimeMetricRow(Base):
    __tablename__ = "skill_runtime_metrics"
    skill_version_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    selection_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful_task_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    user_acceptance_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    user_rejection_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verifier_failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unauthorized_tool_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unsupported_claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_latency_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    total_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_estimated_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0)
