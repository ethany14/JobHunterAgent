"""Career Evidence Vault tables.

Revision ID: 0015_career_evidence
Revises: 0014_workspace_failure
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_career_evidence"
down_revision = "0014_workspace_failure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "career_evidence",
        sa.Column("evidence_id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("rejected_at", sa.DateTime(timezone=True)),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('candidate','confirmed','rejected','superseded','archived')", name="ck_career_evidence_status"),
    )
    op.create_index("ix_career_evidence_status", "career_evidence", ["status"])
    op.create_table(
        "career_evidence_versions",
        sa.Column("evidence_version_id", sa.String(36), primary_key=True),
        sa.Column("evidence_id", sa.String(36), sa.ForeignKey("career_evidence.evidence_id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_reference", sa.String(256)),
        sa.Column("exact_quote", sa.Text()),
        sa.Column("source_section", sa.String(256)),
        sa.Column("employer_or_project", sa.String(256)),
        sa.Column("role", sa.String(256)),
        sa.Column("start_date", sa.String(32)),
        sa.Column("end_date", sa.String(32)),
        sa.Column("technologies_json", sa.Text(), nullable=False),
        sa.Column("metrics_json", sa.Text(), nullable=False),
        sa.Column("tags_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("external_evidence_id", sa.String(64)),
        sa.Column("source_run_id", sa.String(36)),
        sa.UniqueConstraint("evidence_id", "version_number", name="uq_career_evidence_version"),
        sa.UniqueConstraint("source_type", "source_reference", "external_evidence_id", "content_hash", name="uq_career_evidence_import"),
    )
    op.create_index("ix_career_evidence_versions_evidence_id", "career_evidence_versions", ["evidence_id"])
    op.create_index("ix_career_evidence_versions_content_hash", "career_evidence_versions", ["content_hash"])
    op.create_table(
        "career_evidence_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("evidence_id", sa.String(36), sa.ForeignKey("career_evidence.evidence_id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("evidence_version_id", sa.String(36), sa.ForeignKey("career_evidence_versions.evidence_version_id")),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("evidence_id", "sequence", name="uq_career_evidence_event_sequence"),
    )
    op.create_index("ix_career_evidence_events_evidence_id", "career_evidence_events", ["evidence_id"])
    op.create_table(
        "evidence_application_links",
        sa.Column("link_id", sa.String(36), primary_key=True),
        sa.Column("evidence_id", sa.String(36), sa.ForeignKey("career_evidence.evidence_id"), nullable=False),
        sa.Column("evidence_version_id", sa.String(36), sa.ForeignKey("career_evidence_versions.evidence_version_id"), nullable=False),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.application_id", ondelete="CASCADE"), nullable=False),
        sa.Column("requirement_id", sa.String(128)),
        sa.Column("canonical_requirement", sa.String(256)),
        sa.Column("link_type", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.UniqueConstraint("application_id", "evidence_version_id", "requirement_id", name="uq_evidence_application_requirement"),
    )
    op.create_index("ix_evidence_application_links_evidence_id", "evidence_application_links", ["evidence_id"])
    op.create_index("ix_evidence_application_links_application_id", "evidence_application_links", ["application_id"])
    op.execute("CREATE TRIGGER career_evidence_versions_no_update BEFORE UPDATE ON career_evidence_versions BEGIN SELECT RAISE(ABORT, 'evidence versions are immutable'); END")
    op.execute("CREATE TRIGGER career_evidence_versions_no_delete BEFORE DELETE ON career_evidence_versions BEGIN SELECT RAISE(ABORT, 'evidence versions are immutable'); END")
    op.execute("CREATE TRIGGER career_evidence_events_no_update BEFORE UPDATE ON career_evidence_events BEGIN SELECT RAISE(ABORT, 'evidence events are append-only'); END")
    op.execute("CREATE TRIGGER career_evidence_events_no_delete BEFORE DELETE ON career_evidence_events BEGIN SELECT RAISE(ABORT, 'evidence events are append-only'); END")


def downgrade() -> None:
    for name in ("career_evidence_versions_no_update", "career_evidence_versions_no_delete",
                 "career_evidence_events_no_update", "career_evidence_events_no_delete"):
        op.execute(f"DROP TRIGGER {name}")
    op.drop_table("evidence_application_links")
    op.drop_table("career_evidence_events")
    op.drop_table("career_evidence_versions")
    op.drop_table("career_evidence")
