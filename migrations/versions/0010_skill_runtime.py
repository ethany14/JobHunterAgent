"""Add governed Skill registry, versions, and lifecycle events.

Revision ID: 0010_skill_runtime
Revises: 0009_memory_runtime
"""

from alembic import op
import sqlalchemy as sa

revision = "0010_skill_runtime"
down_revision = "0009_memory_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_registry",
        sa.Column("skill_id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("active_version_id", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_skill_registry_name"),
    )
    op.create_index("ix_skill_registry_name", "skill_registry", ["name"])
    op.create_index("ix_skill_registry_active_version_id", "skill_registry", ["active_version_id"])
    op.create_table(
        "skill_versions",
        sa.Column("version_id", sa.String(36), primary_key=True),
        sa.Column("skill_id", sa.String(36), sa.ForeignKey("skill_registry.skill_id", ondelete="CASCADE"), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(1024), nullable=False),
        sa.Column("version_label", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("package_path", sa.Text(), nullable=False),
        sa.Column("license", sa.String(512)),
        sa.Column("compatibility", sa.String(500)),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("allowed_tools_json", sa.Text()),
        sa.Column("instruction_snapshot", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("resource_manifest_json", sa.Text(), nullable=False),
        sa.Column("validation_errors_json", sa.Text(), nullable=False),
        sa.Column("validation_warnings_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("version_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("skill_id", "version_label", name="uq_skill_version_label"),
        sa.CheckConstraint(
            "status IN ('draft','validating','validated','approval_required','active',"
            "'rejected','superseded','retired')",
            name="ck_skill_versions_status",
        ),
    )
    op.create_index("ix_skill_versions_skill_id", "skill_versions", ["skill_id"])
    op.create_index("ix_skill_versions_name", "skill_versions", ["name"])
    op.create_index("ix_skill_versions_status", "skill_versions", ["status"])
    op.create_table(
        "skill_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("skill_versions.version_id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("version_id", "sequence", name="uq_skill_event_sequence"),
    )
    op.create_index("ix_skill_events_version_id", "skill_events", ["version_id"])


def downgrade() -> None:
    op.drop_index("ix_skill_events_version_id", table_name="skill_events")
    op.drop_table("skill_events")
    op.drop_index("ix_skill_versions_status", table_name="skill_versions")
    op.drop_index("ix_skill_versions_name", table_name="skill_versions")
    op.drop_index("ix_skill_versions_skill_id", table_name="skill_versions")
    op.drop_table("skill_versions")
    op.drop_index("ix_skill_registry_active_version_id", table_name="skill_registry")
    op.drop_index("ix_skill_registry_name", table_name="skill_registry")
    op.drop_table("skill_registry")
