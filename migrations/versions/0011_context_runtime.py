"""Add immutable model-context snapshots and usage audit.

Revision ID: 0011_context_runtime
Revises: 0010_skill_runtime
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_context_runtime"
down_revision = "0010_skill_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_sessions") as batch:
        batch.add_column(sa.Column("allowed_skills_json", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("current_context_snapshot_id", sa.String(36)))
        batch.add_column(sa.Column("last_context_snapshot_id", sa.String(36)))
    op.create_index("ix_agent_sessions_current_context_snapshot_id", "agent_sessions", ["current_context_snapshot_id"])
    op.create_index("ix_agent_sessions_last_context_snapshot_id", "agent_sessions", ["last_context_snapshot_id"])
    with op.batch_alter_table("memory_items") as batch:
        batch.add_column(sa.Column("last_used_at", sa.DateTime(timezone=True)))
    op.create_index("ix_memory_items_last_used_at", "memory_items", ["last_used_at"])
    op.create_table(
        "context_snapshots",
        sa.Column("snapshot_id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(128), sa.ForeignKey("agent_sessions.session_id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("abandoned_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('prepared','used','abandoned')", name="ck_context_snapshot_status"),
    )
    op.create_index("ix_context_snapshots_session_id", "context_snapshots", ["session_id"])
    op.create_index("ix_context_snapshots_status", "context_snapshots", ["status"])
    op.create_table(
        "context_snapshot_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("context_snapshots.snapshot_id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("snapshot_id", "sequence", name="uq_context_snapshot_event_sequence"),
    )
    op.create_index("ix_context_snapshot_events_snapshot_id", "context_snapshot_events", ["snapshot_id"])
    op.create_table(
        "memory_usage_events",
        sa.Column("usage_id", sa.String(36), primary_key=True),
        sa.Column("memory_id", sa.String(128), sa.ForeignKey("memory_items.memory_id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("context_snapshots.snapshot_id", ondelete="CASCADE"), nullable=False),
        sa.Column("memory_version", sa.Integer(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_memory_usage_events_memory_id", "memory_usage_events", ["memory_id"])
    op.create_index("ix_memory_usage_events_snapshot_id", "memory_usage_events", ["snapshot_id"])
    op.create_table(
        "skill_usage_events",
        sa.Column("usage_id", sa.String(36), primary_key=True),
        sa.Column("version_id", sa.String(36), sa.ForeignKey("skill_versions.version_id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_id", sa.String(36), sa.ForeignKey("context_snapshots.snapshot_id", ondelete="CASCADE"), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_skill_usage_events_version_id", "skill_usage_events", ["version_id"])
    op.create_index("ix_skill_usage_events_snapshot_id", "skill_usage_events", ["snapshot_id"])


def downgrade() -> None:
    op.drop_index("ix_skill_usage_events_snapshot_id", table_name="skill_usage_events")
    op.drop_index("ix_skill_usage_events_version_id", table_name="skill_usage_events")
    op.drop_table("skill_usage_events")
    op.drop_index("ix_memory_usage_events_snapshot_id", table_name="memory_usage_events")
    op.drop_index("ix_memory_usage_events_memory_id", table_name="memory_usage_events")
    op.drop_table("memory_usage_events")
    op.drop_index("ix_context_snapshot_events_snapshot_id", table_name="context_snapshot_events")
    op.drop_table("context_snapshot_events")
    op.drop_index("ix_context_snapshots_status", table_name="context_snapshots")
    op.drop_index("ix_context_snapshots_session_id", table_name="context_snapshots")
    op.drop_table("context_snapshots")
    op.drop_index("ix_memory_items_last_used_at", table_name="memory_items")
    with op.batch_alter_table("memory_items") as batch:
        batch.drop_column("last_used_at")
    op.drop_index("ix_agent_sessions_last_context_snapshot_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_current_context_snapshot_id", table_name="agent_sessions")
    with op.batch_alter_table("agent_sessions") as batch:
        batch.drop_column("last_context_snapshot_id")
        batch.drop_column("current_context_snapshot_id")
        batch.drop_column("allowed_skills_json")
