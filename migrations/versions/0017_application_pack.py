"""Versioned application packs and evidence snapshots.

Revision ID: 0017_application_pack
Revises: 0016_interviewer_agent
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_application_pack"
down_revision = "0016_interviewer_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("application_packs",
        sa.Column("pack_id", sa.String(36), primary_key=True),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.application_id"), nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("job_snapshots.snapshot_id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("generation_number", sa.Integer(), nullable=False),
        sa.Column("record_version", sa.Integer(), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("evidence_set_hash", sa.String(64), nullable=False),
        sa.Column("preference_snapshot_id", sa.String(36)),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("stale_reason", sa.String(64)), sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("application_id", "generation_number", name="uq_pack_application_version"),
        sa.UniqueConstraint("application_id", "idempotency_key", name="uq_pack_idempotency"))
    op.create_index("ix_application_packs_application_id", "application_packs", ["application_id"])
    op.create_index("ix_application_packs_status", "application_packs", ["status"])
    op.create_table("application_pack_items",
        sa.Column("pack_item_id", sa.String(36), primary_key=True),
        sa.Column("pack_id", sa.String(36), sa.ForeignKey("application_packs.pack_id"), nullable=False),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("application_artifacts.artifact_id")),
        sa.Column("artifact_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_question", sa.Text()), sa.Column("max_length", sa.Integer()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("revision_count", sa.Integer(), nullable=False),
        sa.Column("max_revisions", sa.Integer(), nullable=False),
        sa.Column("verification_json", sa.Text()),
        sa.Column("requires_manual_answer", sa.Boolean(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("pack_id", "idempotency_key", name="uq_pack_item_idempotency"))
    op.create_index("ix_application_pack_items_pack_id", "application_pack_items", ["pack_id"])
    op.create_table("generation_evidence_snapshots",
        sa.Column("generation_snapshot_id", sa.String(36), primary_key=True),
        sa.Column("pack_id", sa.String(36), sa.ForeignKey("application_packs.pack_id"), nullable=False, unique=True),
        sa.Column("application_id", sa.String(36), sa.ForeignKey("applications.application_id"), nullable=False),
        sa.Column("job_snapshot_id", sa.String(36), sa.ForeignKey("job_snapshots.snapshot_id"), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("evidence_set_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("generation_evidence_snapshot_items",
        sa.Column("generation_snapshot_id", sa.String(36), sa.ForeignKey("generation_evidence_snapshots.generation_snapshot_id"), primary_key=True),
        sa.Column("evidence_id", sa.String(36), primary_key=True),
        sa.Column("evidence_version_id", sa.String(36), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("selection_reason", sa.String(64), nullable=False),
        sa.Column("associated_requirement_ids_json", sa.Text(), nullable=False))
    op.create_table("application_pack_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("pack_id", sa.String(36), sa.ForeignKey("application_packs.pack_id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("pack_id", "sequence", name="uq_pack_event_sequence"),
        sa.UniqueConstraint("pack_id", "idempotency_key", name="uq_pack_event_idempotency"))
    op.create_index("ix_application_pack_events_pack_id", "application_pack_events", ["pack_id"])


def downgrade() -> None:
    op.drop_table("application_pack_events")
    op.drop_table("generation_evidence_snapshot_items")
    op.drop_table("generation_evidence_snapshots")
    op.drop_table("application_pack_items")
    op.drop_table("application_packs")
