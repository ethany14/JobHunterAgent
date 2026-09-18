"""Add governed memory items and audit events.

Revision ID: 0009_memory_runtime
Revises: 0008_session_cancellation_deadlines
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_memory_runtime"
down_revision = "0008_session_cancellation_deadlines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("memory_id", sa.String(128), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(128), nullable=False),
        sa.Column("memory_key", sa.String(160), nullable=False),
        sa.Column("memory_type", sa.String(16), nullable=False),
        sa.Column("display_text", sa.Text(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("sensitivity", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("supersedes_memory_id", sa.String(128), sa.ForeignKey("memory_items.memory_id", ondelete="SET NULL")),
        sa.Column("superseded_by_memory_id", sa.String(128), sa.ForeignKey("memory_items.memory_id", ondelete="SET NULL")),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("item_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("memory_type IN ('semantic','preference','episodic')", name="ck_memory_items_type"),
        sa.CheckConstraint("scope_type IN ('user','project','session')", name="ck_memory_items_scope"),
        sa.CheckConstraint("status IN ('candidate','confirmed','rejected','superseded','deleted','expired')", name="ck_memory_items_status"),
        sa.CheckConstraint("sensitivity IN ('normal','personal','sensitive')", name="ck_memory_items_sensitivity"),
    )
    for column in ("owner_id", "scope_type", "scope_id", "memory_key", "status", "expires_at"):
        op.create_index(f"ix_memory_items_{column}", "memory_items", [column])
    op.create_index(
        "uq_memory_active_confirmed_key",
        "memory_items",
        ["owner_id", "scope_type", "scope_id", "memory_key"],
        unique=True,
        sqlite_where=sa.text("status = 'confirmed'"),
    )
    op.create_table(
        "memory_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("memory_id", sa.String(128), sa.ForeignKey("memory_items.memory_id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("from_status", sa.String(16)),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("memory_id", "sequence", name="uq_memory_event_sequence"),
    )
    op.create_index("ix_memory_events_memory_id", "memory_events", ["memory_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_events_memory_id", table_name="memory_events")
    op.drop_table("memory_events")
    op.drop_index("uq_memory_active_confirmed_key", table_name="memory_items")
    for column in ("expires_at", "status", "memory_key", "scope_id", "scope_type", "owner_id"):
        op.drop_index(f"ix_memory_items_{column}", table_name="memory_items")
    op.drop_table("memory_items")
