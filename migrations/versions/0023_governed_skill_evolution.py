"""Add governed Skill evolution records without changing historical Skill rows.

Revision ID: 0023_governed_skill_evolution
Revises: 0022_feedback_learning
"""
from alembic import op
import sqlalchemy as sa

revision = "0023_governed_skill_evolution"
down_revision = "0022_feedback_learning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("skill_evolution_candidates",
        sa.Column("candidate_id", sa.String(36), sa.ForeignKey("learning_candidates.candidate_id"), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("staged_path", sa.Text()), sa.Column("staged_hash", sa.String(64)),
        sa.Column("skill_name", sa.String(64)), sa.Column("semantic_version", sa.String(64)),
        sa.Column("evaluation_run_id", sa.String(36)),
        sa.Column("published_version_id", sa.String(36)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_skill_evolution_candidates_owner_id", "skill_evolution_candidates", ["owner_id"])
    op.create_table("skill_evolution_versions",
        sa.Column("skill_version_id", sa.String(36), primary_key=True),
        sa.Column("skill_name", sa.String(64), nullable=False),
        sa.Column("semantic_version", sa.String(64), nullable=False),
        sa.Column("parent_version_id", sa.String(36)),
        sa.Column("candidate_id", sa.String(36), sa.ForeignKey("learning_candidates.candidate_id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("activation_mode", sa.String(16), nullable=False),
        sa.Column("package_path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("evaluation_run_id", sa.String(36), nullable=False),
        sa.Column("published_by", sa.String(128), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("skill_name", "semantic_version"))
    op.create_index("ix_skill_evolution_versions_skill_name", "skill_evolution_versions", ["skill_name"])
    op.create_table("skill_evaluation_runs",
        sa.Column("evaluation_run_id", sa.String(36), primary_key=True),
        sa.Column("candidate_id", sa.String(36), sa.ForeignKey("learning_candidates.candidate_id"), nullable=False),
        sa.Column("baseline_skill_version_id", sa.String(36)),
        sa.Column("staged_skill_version_id", sa.String(36), nullable=False),
        sa.Column("staged_hash", sa.String(64), nullable=False),
        sa.Column("dataset_id", sa.String(128), nullable=False),
        sa.Column("dataset_version", sa.String(64), nullable=False),
        sa.Column("dataset_hash", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=False),
        sa.Column("repetitions", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("summary_json", sa.Text()),
        sa.Column("error_code", sa.String(64)))
    op.create_index("ix_skill_evaluation_runs_candidate_id", "skill_evaluation_runs", ["candidate_id"])
    op.create_table("skill_evaluation_case_results",
        sa.Column("result_id", sa.String(36), primary_key=True),
        sa.Column("evaluation_run_id", sa.String(36), sa.ForeignKey("skill_evaluation_runs.evaluation_run_id"), nullable=False),
        sa.Column("case_id", sa.String(128), nullable=False),
        sa.Column("variant", sa.String(16), nullable=False),
        sa.Column("repetition", sa.Integer(), nullable=False),
        sa.Column("output_reference", sa.String(128)),
        sa.Column("output_text", sa.Text()),
        sa.Column("metric_values_json", sa.Text(), nullable=False),
        sa.Column("latency", sa.Float(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost", sa.Float()),
        sa.Column("safety_violations", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("evaluation_run_id", "case_id", "variant", "repetition"))
    op.create_index("ix_skill_evaluation_case_results_evaluation_run_id", "skill_evaluation_case_results", ["evaluation_run_id"])
    op.create_table("skill_forward_test_results",
        sa.Column("result_id", sa.String(36), primary_key=True),
        sa.Column("evaluation_run_id", sa.String(36), sa.ForeignKey("skill_evaluation_runs.evaluation_run_id"), nullable=False),
        sa.Column("case_id", sa.String(128), nullable=False),
        sa.Column("metric_values_json", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("skill_activation_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("skill_version_id", sa.String(36)),
        sa.Column("candidate_id", sa.String(36)),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("reviewer", sa.String(128)),
        sa.Column("reason_code", sa.String(64)),
        sa.Column("request_key", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("candidate_id", "request_key"))
    op.create_table("skill_runtime_metrics",
        sa.Column("skill_version_id", sa.String(36), primary_key=True),
        sa.Column("selection_count", sa.Integer(), nullable=False),
        sa.Column("successful_task_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("user_acceptance_count", sa.Integer(), nullable=False),
        sa.Column("user_rejection_count", sa.Integer(), nullable=False),
        sa.Column("verifier_failure_count", sa.Integer(), nullable=False),
        sa.Column("unauthorized_tool_attempt_count", sa.Integer(), nullable=False),
        sa.Column("unsupported_claim_count", sa.Integer(), nullable=False),
        sa.Column("total_latency_seconds", sa.Float(), nullable=False),
        sa.Column("total_input_tokens", sa.Integer(), nullable=False),
        sa.Column("total_output_tokens", sa.Integer(), nullable=False),
        sa.Column("total_estimated_cost", sa.Float(), nullable=False))


def downgrade() -> None:
    raise RuntimeError("Governed Skill evolution migration is forward-only.")
