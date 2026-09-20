"""Create persistent Job Workspace domain.

Revision ID: 0013_job_workspace
Revises: 0012_context_management
"""

from alembic import op
import sqlalchemy as sa

revision = "0013_job_workspace"
down_revision = "0012_context_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("jobs",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("canonical_url", sa.String(2048), unique=True),
        sa.Column("source_site", sa.String(128)), sa.Column("company", sa.String(256)),
        sa.Column("title", sa.String(256)), sa.Column("location", sa.String(256)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    for name, cols in (("ix_jobs_canonical_url", ["canonical_url"]), ("ix_jobs_company_title", ["company", "title"])):
        op.create_index(name, "jobs", cols)
    op.create_table("job_snapshots",
        sa.Column("snapshot_id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.job_id"), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("raw_page_text", sa.Text()), sa.Column("cleaned_job_description", sa.Text(), nullable=False),
        sa.Column("extraction_metadata_json", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", "content_hash", name="uq_job_snapshot_content"))
    op.create_index("ix_job_snapshots_job_id", "job_snapshots", ["job_id"])
    op.create_index("ix_job_snapshots_content_hash", "job_snapshots", ["content_hash"])
    statuses = "'saved','analyzing','needs_evidence','materials_ready','ready_to_apply','applied','interviewing','offer','rejected','withdrawn','archived'"
    op.create_table("applications",
        sa.Column("application_id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.job_id"), nullable=False),
        sa.Column("current_snapshot_id", sa.String(36), sa.ForeignKey("job_snapshots.snapshot_id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False), sa.Column("next_action", sa.Text()),
        sa.Column("deadline_at", sa.DateTime(timezone=True)), sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"status IN ({statuses})", name="ck_applications_status"))
    op.create_index("ix_applications_status", "applications", ["status"])
    op.create_index("ix_applications_job_id", "applications", ["job_id"])
    op.create_index("ix_applications_current_snapshot_id", "applications", ["current_snapshot_id"])
    op.create_table("application_artifacts",
        sa.Column("artifact_id", sa.String(36), primary_key=True),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.application_id", ondelete="CASCADE"), nullable=False),
        sa.Column("artifact_type", sa.String(32), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False), sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("evidence_ids_json", sa.Text(), nullable=False), sa.Column("verification_status", sa.String(64)),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("source_run_id", sa.String(36), sa.ForeignKey("runs.run_id")),
        sa.Column("source_session_id", sa.String(128), sa.ForeignKey("agent_sessions.session_id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("application_id", "artifact_type", "version", name="uq_application_artifact_version"),
        sa.UniqueConstraint("application_id", "artifact_type", "source_run_id", name="uq_application_artifact_source_run"),
        sa.CheckConstraint("status IN ('draft','verified','approved','superseded')", name="ck_application_artifact_status"))
    op.create_index("ix_application_artifacts_application_id", "application_artifacts", ["application_id"])
    op.create_table("application_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.application_id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False), sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False), sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("application_id", "sequence", name="uq_application_event_sequence"))
    op.create_index("ix_application_events_application_id", "application_events", ["application_id"])
    op.create_table("application_runs",
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.application_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.run_id"), primary_key=True),
        sa.Column("role", sa.String(64), nullable=False), sa.Column("attached_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("application_id", "run_id", name="uq_application_run"))
    op.create_table("application_sessions",
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.application_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("session_id", sa.String(128), sa.ForeignKey("agent_sessions.session_id"), primary_key=True),
        sa.Column("role", sa.String(64), nullable=False), sa.Column("attached_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("application_id", "session_id", name="uq_application_session"))


def downgrade() -> None:
    for table in ("application_sessions", "application_runs", "application_events", "application_artifacts", "applications", "job_snapshots", "jobs"):
        op.drop_table(table)
